//! Database & Branch reconcilers — mk-compute.sh, promoted to a controller.
//!
//! Identity discipline (RFC 012): tenant/timeline IDs derive deterministically
//! from the CR's UID, so replays and crashes converge on the same cell-side
//! resources with no state carried between attempts. Child objects (ConfigMap,
//! Pod, Service) carry owner references — Kubernetes GC deletes them with the
//! CR; the finalizer only handles cell-side cleanup.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context as _;
use futures::StreamExt;
use k8s_openapi::api::core::v1::{ConfigMap, Pod, Service};
use kube::api::{Api, ListParams, Patch, PatchParams};
use kube::runtime::controller::{Action, Controller};
use kube::runtime::finalizer::{Event as Finalizer, finalizer};
use kube::runtime::watcher;
use kube::{Client, Resource, ResourceExt};
use serde_json::json;
use sha2::{Digest, Sha256};
use tracing::{info, warn};

use crate::crd::{Branch, Database, Phase, Priority};
use crate::keys::ComputeKey;
use crate::ports;
use crate::spec::{SpecParams, render};
use crate::storcon::Storcon;

pub const FINALIZER: &str = "sspc.io/cell-cleanup";
pub const ENDPOINT_LABEL: &str = "sspc.io/endpoint";
/// RFC3339 UTC timestamp; wake requests newer than `suspendedAt` win.
pub const WAKE_ANNOTATION: &str = "sspc.io/wake-requested-at";

pub fn now_ts() -> String {
    chrono::Utc::now()
        .to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// Suspend-awareness: a resource runs unless it is Suspended with no wake
/// request newer than the suspension. Fixed-format UTC RFC3339 strings
/// compare chronologically, so this is monotonic with no annotation clearing.
pub fn wants_running(
    meta: &kube::api::ObjectMeta,
    status: Option<&crate::crd::EndpointStatus>,
) -> bool {
    let Some(s) = status else { return true };
    if s.phase != Some(Phase::Suspended) {
        return true;
    }
    let suspended_at = s.suspended_at.as_deref().unwrap_or("");
    meta.annotations
        .as_ref()
        .and_then(|a| a.get(WAKE_ANNOTATION))
        .is_some_and(|w| w.as_str() > suspended_at)
}

pub struct Ctx {
    pub client: Client,
    pub storcon: Storcon,
    pub key: ComputeKey,
    pub namespace: String,
    pub compute_image: String,
    pub image_pull_policy: String,
    pub safekeepers: String,
    pub pageserver_connstring: String,
    pub pg_password: String,
    /// name -> ring of (epoch_secs, cpu_millis, mem_mib); ~10 min at 15s ticks.
    pub metrics: std::sync::Mutex<std::collections::HashMap<String, std::collections::VecDeque<(i64, i64, i64)>>>,
}

/// Post a lifecycle Event on a CR — the audit line (013 ticker, 009 seed).
pub async fn post_event(
    ctx: &Ctx,
    api_version: &str,
    kind: &str,
    name: &str,
    uid: Option<String>,
    reason: &str,
    message: String,
) {
    let ev = k8s_openapi::api::core::v1::Event {
        metadata: kube::api::ObjectMeta {
            name: Some(format!("{name}-{}-{}", reason.to_lowercase(), chrono::Utc::now().timestamp())),
            namespace: Some(ctx.namespace.clone()),
            ..Default::default()
        },
        type_: Some("Normal".into()),
        reason: Some(reason.into()),
        message: Some(message),
        involved_object: k8s_openapi::api::core::v1::ObjectReference {
            api_version: Some(api_version.into()),
            kind: Some(kind.into()),
            name: Some(name.into()),
            namespace: Some(ctx.namespace.clone()),
            uid,
            ..Default::default()
        },
        ..Default::default()
    };
    let api: Api<k8s_openapi::api::core::v1::Event> =
        Api::namespaced(ctx.client.clone(), &ctx.namespace);
    if let Err(e) = api.create(&kube::api::PostParams::default(), &ev).await {
        warn!("event {reason} for {name}: {e}");
    }
}

/// CU/priority → pod resources + PriorityClass (RFC 011 QoS, compute layer).
/// Request = priority-weighted fraction of the limit: that fraction IS the
/// contention share under CFS, so throttling follows priority automatically.
pub fn class_resources(cu_limit: i64, priority: Priority) -> (serde_json::Value, &'static str) {
    let cu = cu_limit.clamp(1, 960);
    let cpu_limit_m = cu * 100;
    let (div, class) = match priority {
        Priority::High => (5, "sspc-high"),
        Priority::Standard => (10, "sspc-standard"),
        Priority::Low => (20, "sspc-low"),
    };
    let cpu_req_m = (cpu_limit_m / div).max(10);
    let mem_limit_mi = (cu * 100).max(512);
    let mem_req_mi = mem_limit_mi.min(256);
    (
        json!({
            "requests": {"cpu": format!("{cpu_req_m}m"), "memory": format!("{mem_req_mi}Mi")},
            "limits": {"cpu": format!("{cpu_limit_m}m"), "memory": format!("{mem_limit_mi}Mi")},
        }),
        class,
    )
}

/// Parse "0/29E2300" into a comparable number.
fn lsn_num(lsn: &str) -> u64 {
    let mut it = lsn.splitn(2, '/');
    let hi = u64::from_str_radix(it.next().unwrap_or("0"), 16).unwrap_or(0);
    let lo = u64::from_str_radix(it.next().unwrap_or("0"), 16).unwrap_or(0);
    (hi << 32) | lo
}

/// The parent compute's current flush LSN, via in-cluster SQL.
async fn parent_flush_lsn(ctx: &Ctx, db_name: &str) -> Option<String> {
    let mut cfg = tokio_postgres::Config::new();
    cfg.host(format!("{db_name}.{}.svc.cluster.local", ctx.namespace))
        .port(55433)
        .user("cloud_admin")
        .password(&ctx.pg_password)
        .dbname("postgres")
        .application_name("sspc-operator")
        .connect_timeout(Duration::from_secs(3));
    let (client, conn) = cfg.connect(tokio_postgres::NoTls).await.ok()?;
    let handle = tokio::spawn(conn);
    let row = client
        .query_one("SELECT pg_current_wal_flush_lsn()::text", &[])
        .await
        .ok()?;
    let lsn: String = row.get(0);
    drop(client);
    handle.abort();
    Some(lsn)
}

/// 32-hex deterministic id from a CR UID and a salt.
pub fn derive_id(uid: &str, salt: &str) -> String {
    hex::encode(&Sha256::digest(format!("{uid}:{salt}").as_bytes())[..16])
}

fn owner_ref<K>(obj: &K) -> serde_json::Value
where
    K: Resource<DynamicType = ()>,
{
    json!({
        "apiVersion": K::api_version(&()),
        "kind": K::kind(&()),
        "name": obj.name_any(),
        "uid": obj.meta().uid.clone().unwrap_or_default(),
        "controller": true,
    })
}

impl Ctx {
    async fn used_node_ports(&self, exclude_svc: &str) -> anyhow::Result<BTreeSet<i32>> {
        let svcs: Api<Service> = Api::namespaced(self.client.clone(), &self.namespace);
        let list = svcs
            .list(&ListParams::default().labels(ENDPOINT_LABEL))
            .await?;
        Ok(list
            .items
            .iter()
            .filter(|s| s.name_any() != exclude_svc)
            .filter_map(|s| s.spec.as_ref()?.ports.as_ref()?.first()?.node_port)
            .collect())
    }

    /// The shared ensure-flow for a connectable endpoint (Database or Branch).
    /// `run_pod: false` keeps ConfigMap + Service (sticky port) but does not
    /// (re)create the compute — the suspended state (RFC 012 D5).
    async fn ensure_endpoint<K>(
        &self,
        obj: &K,
        name: &str,
        tenant: &str,
        timeline: &str,
        run_pod: bool,
        cu_limit: i64,
        priority: Priority,
    ) -> anyhow::Result<i32>
    where
        K: Resource<DynamicType = ()>,
    {
        let ns = &self.namespace;
        let pp = PatchParams::apply("sspc-operator").force();
        let oref = owner_ref(obj);

        // Spec ConfigMap
        let spec_json = render(&SpecParams {
            tenant_id: tenant,
            timeline_id: timeline,
            jwks_x_b64url: &self.key.x_b64url,
            jwks_kid_b64url: &self.key.kid_b64url,
            safekeepers: &self.safekeepers,
            pageserver_connstring: &self.pageserver_connstring,
        })?;
        let cm: ConfigMap = serde_json::from_value(json!({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": format!("{name}-spec"), "namespace": ns,
                          "ownerReferences": [oref]},
            "data": {"spec.json": serde_json::to_string_pretty(&spec_json)?},
        }))?;
        Api::<ConfigMap>::namespaced(self.client.clone(), ns)
            .patch(&cm.name_any(), &pp, &Patch::Apply(&cm))
            .await?;

        // NodePort Service (stable pick, probe past collisions)
        let svcs: Api<Service> = Api::namespaced(self.client.clone(), ns);
        let existing_port = svcs
            .get_opt(name)
            .await?
            .and_then(|s| s.spec?.ports?.first()?.node_port);
        let port = match existing_port {
            Some(p) => p,
            None => {
                let used = self.used_node_ports(name).await?;
                ports::pick(name, &used)
                    .context("endpoint NodePort block exhausted (M1 cap, RFC 012 D3)")?
            }
        };
        let svc: Service = serde_json::from_value(json!({
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns,
                          "labels": {ENDPOINT_LABEL: "true"},
                          "ownerReferences": [oref]},
            "spec": {"type": "NodePort", "selector": {"sspc.io/compute": name},
                      "ports": [{"name": "pg", "port": 55433, "nodePort": port}]},
        }))?;
        svcs.patch(name, &pp, &Patch::Apply(&svc)).await?;

        if !run_pod {
            return Ok(port);
        }
        let (resources, priority_class) = class_resources(cu_limit, priority);
        // Compute pod: compute_ctl as PID 1 (D6), stock image, spec from the CM.
        let pod: Pod = serde_json::from_value(json!({
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": ns,
                          "labels": {"app": "compute", "sspc.io/compute": name},
                          "ownerReferences": [oref]},
            "spec": {
                "priorityClassName": priority_class,
                "containers": [{
                    "name": "compute",
                    "image": self.compute_image,
                    "imagePullPolicy": self.image_pull_policy,
                    "command": [
                        "compute_ctl",
                        "--pgdata=/var/db/postgres/compute",
                        "--connstr=postgresql://cloud_admin@localhost:55433/postgres",
                        "--pgbin=/usr/local/bin/postgres",
                        format!("--compute-id={name}"),
                        "--config=/config/spec.json",
                    ],
                    "resources": resources,
                    "ports": [{"containerPort": 55433}, {"containerPort": 3080}],
                    "volumeMounts": [{"name": "spec", "mountPath": "/config"}],
                    // pg_isready, not tcpSocket: PG briefly accepts TCP but
                    // resets connections during startup (found by T4).
                    "readinessProbe": {
                        "exec": {"command": ["pg_isready", "-h", "localhost",
                                              "-p", "55433", "-U", "cloud_admin"]},
                        "periodSeconds": 1,
                    },
                }],
                "volumes": [{"name": "spec", "configMap": {"name": format!("{name}-spec")}}],
            },
        }))?;
        Api::<Pod>::namespaced(self.client.clone(), ns)
            .patch(name, &pp, &Patch::Apply(&pod))
            .await?;

        Ok(port)
    }

    async fn patch_status<K>(&self, api: &Api<K>, name: &str, status: serde_json::Value)
    where
        K: Resource<DynamicType = ()> + Clone + serde::de::DeserializeOwned + std::fmt::Debug,
    {
        let _ = api
            .patch_status(
                name,
                &PatchParams::default(),
                &Patch::Merge(json!({"status": status})),
            )
            .await
            .map_err(|e| warn!("status patch failed for {name}: {e}"));
    }
}

// ---------- Database ----------

async fn reconcile_db(db: Arc<Database>, ctx: Arc<Ctx>) -> Result<Action, kube::runtime::finalizer::Error<kube::Error>> {
    let api: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    finalizer(&api, FINALIZER, db, |event| async {
        match event {
            Finalizer::Apply(db) => apply_db(db, &ctx).await.map_err(to_kube_err),
            Finalizer::Cleanup(db) => cleanup_db(db, &ctx).await.map_err(to_kube_err),
        }
    })
    .await
}

fn to_kube_err(e: anyhow::Error) -> kube::Error {
    kube::Error::Service(e.into())
}

async fn apply_db(db: Arc<Database>, ctx: &Ctx) -> anyhow::Result<Action> {
    let name = db.name_any();
    let uid = db.meta().uid.clone().context("no uid")?;
    let tenant = derive_id(&uid, "tenant");
    let timeline = derive_id(&uid, "root");

    let was = db.status.as_ref().and_then(|s| s.phase);
    ctx.storcon.create_tenant(&tenant).await?;
    ctx.storcon.create_timeline(&tenant, &timeline, None).await?;
    let run = wants_running(db.meta(), db.status.as_ref());
    if was.is_none() {
        post_event(ctx, "sspc.io/v1alpha1", "Database", &name, db.meta().uid.clone(),
                   "Created", format!("database {name} provisioned")).await;
    } else if was == Some(Phase::Suspended) && run {
        post_event(ctx, "sspc.io/v1alpha1", "Database", &name, db.meta().uid.clone(),
                   "Woke", format!("database {name} woke from suspend")).await;
    }
    let port = ctx
        .ensure_endpoint(db.as_ref(), &name, &tenant, &timeline, run, db.spec.cu_limit, db.spec.priority)
        .await?;

    let phase = if run { Phase::Active } else { Phase::Suspended };
    let api: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    ctx.patch_status(&api, &name, json!({
        "phase": phase, "tenantId": tenant, "timelineId": timeline, "nodePort": port,
    }))
    .await;
    info!("database {name} reconciled: tenant={tenant} timeline={timeline} port={port} run={run}");
    Ok(Action::requeue(Duration::from_secs(300)))
}

async fn cleanup_db(db: Arc<Database>, ctx: &Ctx) -> anyhow::Result<Action> {
    let uid = db.meta().uid.clone().context("no uid")?;
    let tenant = derive_id(&uid, "tenant");
    ctx.storcon.delete_tenant(&tenant).await?;
    info!("database {} cleaned up (tenant {tenant})", db.name_any());
    Ok(Action::await_change())
}

// ---------- Branch ----------

async fn reconcile_branch(br: Arc<Branch>, ctx: Arc<Ctx>) -> Result<Action, kube::runtime::finalizer::Error<kube::Error>> {
    let api: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    finalizer(&api, FINALIZER, br, |event| async {
        match event {
            Finalizer::Apply(br) => apply_branch(br, &ctx).await.map_err(to_kube_err),
            Finalizer::Cleanup(br) => cleanup_branch(br, &ctx).await.map_err(to_kube_err),
        }
    })
    .await
}

async fn apply_branch(br: Arc<Branch>, ctx: &Ctx) -> anyhow::Result<Action> {
    let name = br.name_any();
    let uid = br.meta().uid.clone().context("no uid")?;
    let api_db: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);

    let parent = api_db.get(&br.spec.database).await?;
    let Some(status) = parent.status.as_ref() else {
        return Ok(Action::requeue(Duration::from_secs(3)));
    };
    let (Some(tenant), Some(ancestor)) = (status.tenant_id.clone(), status.timeline_id.clone())
    else {
        return Ok(Action::requeue(Duration::from_secs(3)));
    };

    let was = br.status.as_ref().and_then(|s| s.phase);
    let timeline = derive_id(&uid, "branch");
    // Branch-at-head race (found by T4): the timeline branches at the
    // pageserver's INGESTED lsn, which can lag the parent's just-flushed
    // writes — a branch created immediately after a load would miss it.
    // If the parent is awake, wait (bounded) for ingestion to catch up.
    if was.is_none() && parent.status.as_ref().and_then(|s| s.phase) == Some(Phase::Active) {
        // Fail closed: an unreadable flush LSN is usually Service-endpoint
        // propagation lag on a parent that just woke — branching blind at the
        // ingested LSN would silently drop the parent's latest writes.
        let Some(flush) = parent_flush_lsn(ctx, &br.spec.database).await else {
            warn!("branch {name}: parent flush lsn unreadable; requeueing instead of branching blind");
            return Ok(Action::requeue(Duration::from_secs(2)));
        };
        let deadline = std::time::Instant::now() + Duration::from_secs(20);
        loop {
            let ingested = ctx.storcon.timeline_last_record_lsn(&tenant, &ancestor).await;
            match &ingested {
                Some(i) if lsn_num(i) >= lsn_num(&flush) => {
                    info!("branch {name}: ingestion caught up ({i} >= flush {flush})");
                    break;
                }
                _ if std::time::Instant::now() > deadline => {
                    warn!("branch {name}: ingestion lag past deadline (flush {flush}, ingested {ingested:?}); branching anyway");
                    break;
                }
                _ => {
                    info!("branch {name}: waiting for ingestion ({ingested:?} < flush {flush})");
                    tokio::time::sleep(Duration::from_millis(250)).await;
                }
            }
        }
    }
    ctx.storcon
        .create_timeline(&tenant, &timeline, Some(&ancestor))
        .await?;
    let run = wants_running(br.meta(), br.status.as_ref());
    if was.is_none() {
        post_event(ctx, "sspc.io/v1alpha1", "Branch", &name, br.meta().uid.clone(),
                   "Created", format!("branch {name} of {} created", br.spec.database)).await;
    } else if was == Some(Phase::Suspended) && run {
        post_event(ctx, "sspc.io/v1alpha1", "Branch", &name, br.meta().uid.clone(),
                   "Woke", format!("branch {name} woke from suspend")).await;
    }
    let port = ctx
        .ensure_endpoint(br.as_ref(), &name, &tenant, &timeline, run, br.spec.cu_limit, br.spec.priority)
        .await?;

    let phase = if run { Phase::Active } else { Phase::Suspended };
    let api: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    ctx.patch_status(&api, &name, json!({
        "phase": phase, "tenantId": tenant, "timelineId": timeline, "nodePort": port,
    }))
    .await;
    info!("branch {name} reconciled: timeline={timeline} (ancestor {ancestor}) port={port}");
    Ok(Action::requeue(Duration::from_secs(300)))
}

async fn cleanup_branch(br: Arc<Branch>, ctx: &Ctx) -> anyhow::Result<Action> {
    let uid = br.meta().uid.clone().context("no uid")?;
    let timeline = derive_id(&uid, "branch");
    // Parent may already be gone (tenant delete removes all timelines) — 404 is fine.
    if let Some(tenant) = br.status.as_ref().and_then(|s| s.tenant_id.as_ref()) {
        ctx.storcon.delete_timeline(tenant, &timeline).await?;
    }
    info!("branch {} cleaned up (timeline {timeline})", br.name_any());
    Ok(Action::await_change())
}

// ---------- runners ----------

fn error_policy<K>(_obj: Arc<K>, err: &kube::runtime::finalizer::Error<kube::Error>, _ctx: Arc<Ctx>) -> Action {
    warn!("reconcile error: {err:?}");
    Action::requeue(Duration::from_secs(5))
}

pub async fn run(ctx: Arc<Ctx>) -> anyhow::Result<()> {
    let dbs: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let brs: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);

    let db_ctrl = Controller::new(dbs, watcher::Config::default())
        .run(reconcile_db, error_policy, ctx.clone())
        .for_each(|_| async {});
    let br_ctrl = Controller::new(brs, watcher::Config::default())
        .run(reconcile_branch, error_policy, ctx.clone())
        .for_each(|_| async {});

    futures::join!(db_ctrl, br_ctrl);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crd::EndpointStatus;

    fn status(phase: Phase, suspended_at: Option<&str>) -> EndpointStatus {
        EndpointStatus {
            phase: Some(phase),
            suspended_at: suspended_at.map(String::from),
            ..Default::default()
        }
    }

    fn meta_with_wake(at: Option<&str>) -> kube::api::ObjectMeta {
        kube::api::ObjectMeta {
            annotations: at.map(|t| {
                [(WAKE_ANNOTATION.to_string(), t.to_string())].into()
            }),
            ..Default::default()
        }
    }

    #[test]
    fn wake_wins_only_when_newer_than_suspension() {
        let m_none = meta_with_wake(None);
        let m_old = meta_with_wake(Some("2026-08-09T10:00:00Z"));
        let m_new = meta_with_wake(Some("2026-08-09T12:00:00Z"));
        let s = status(Phase::Suspended, Some("2026-08-09T11:00:00Z"));

        assert!(!wants_running(&m_none, Some(&s)), "suspended, no wake");
        assert!(!wants_running(&m_old, Some(&s)), "stale wake loses");
        assert!(wants_running(&m_new, Some(&s)), "fresh wake wins");
        assert!(
            wants_running(&m_none, Some(&status(Phase::Active, None))),
            "non-suspended always runs"
        );
        assert!(wants_running(&m_none, None), "no status yet -> provision");
    }

    #[test]
    fn class_resources_follow_priority() {
        let (hi, hic) = class_resources(10, Priority::High);
        let (st, stc) = class_resources(10, Priority::Standard);
        let (lo, loc) = class_resources(10, Priority::Low);
        assert_eq!((hic, stc, loc), ("sspc-high", "sspc-standard", "sspc-low"));
        assert_eq!(hi["limits"]["cpu"], "1000m");
        assert_eq!(hi["requests"]["cpu"], "200m");
        assert_eq!(st["requests"]["cpu"], "100m");
        assert_eq!(lo["requests"]["cpu"], "50m");
        let (big, _) = class_resources(200, Priority::Standard);
        assert_eq!(big["limits"]["cpu"], "20000m");
        assert_eq!(big["limits"]["memory"], "20000Mi");
        let (tiny, _) = class_resources(1, Priority::Low);
        assert_eq!(tiny["requests"]["cpu"], "10m");
        assert_eq!(tiny["limits"]["memory"], "512Mi");
    }

    #[test]
    fn derived_ids_are_stable_32_hex_and_distinct() {
        let a = derive_id("uid-1", "tenant");
        let b = derive_id("uid-1", "root");
        let c = derive_id("uid-2", "tenant");
        assert_eq!(a.len(), 32);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(a, derive_id("uid-1", "tenant"), "replay converges");
        assert_ne!(a, b, "tenant and timeline differ");
        assert_ne!(a, c, "different CRs differ");
    }
}
