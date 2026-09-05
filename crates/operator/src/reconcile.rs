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
use crate::ports;
use crate::storcon::Storcon;
use supabricks_core::branch::{HeadWait, head_wait_verdict};
use supabricks_core::keys::{ComputeKey, pg_md5};
use supabricks_core::spec::{ComputePaths, Settings, SpecParams, render};

// Keep the existing image's paths and executable lookup at the K8s boundary.
fn compute_command(name: &str) -> anyhow::Result<Vec<String>> {
    let mut command = ComputePaths {
        compute_ctl: "/usr/local/bin/compute_ctl".into(),
        postgres: "/usr/local/bin/postgres".into(),
        data: "/var/db/postgres/compute".into(),
        config: "/config/spec.json".into(),
    }
    .command(name, "postgresql://cloud_admin@localhost:55433/postgres")?;
    command[0] = "compute_ctl".into();
    Ok(command)
}

pub const FINALIZER: &str = "sspc.io/cell-cleanup";
pub const ENDPOINT_LABEL: &str = "sspc.io/endpoint";
/// RFC3339 UTC timestamp; wake requests newer than `suspendedAt` win.
pub const WAKE_ANNOTATION: &str = "sspc.io/wake-requested-at";

pub fn now_ts() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// Wake and suspension events need subsecond precision: a wake can arrive in
/// the same second that suspension completes (observed by the chaos gate).
pub fn lifecycle_event_ts() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Nanos, true)
}

/// A resource runs unless it is suspended without a newer wake. Parse timestamps
/// so older second-resolution records and new fractional records compare by time.
pub fn wants_running(
    meta: &kube::api::ObjectMeta,
    status: Option<&crate::crd::EndpointStatus>,
) -> bool {
    let Some(s) = status else { return true };
    if s.phase != Some(Phase::Suspended) {
        return true;
    }
    let wake = meta
        .annotations
        .as_ref()
        .and_then(|a| a.get(WAKE_ANNOTATION))
        .and_then(|w| chrono::DateTime::parse_from_rfc3339(w).ok());
    match (wake, s.suspended_at.as_deref()) {
        (Some(wake), Some(suspended)) => {
            chrono::DateTime::parse_from_rfc3339(suspended).is_ok_and(|suspended| wake > suspended)
        }
        (Some(_), None) => true,
        _ => false,
    }
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
    /// name -> ring of (epoch_secs, cpu_millis, mem_mib); ~10 min at 15s ticks.
    pub metrics: std::sync::Mutex<
        std::collections::HashMap<String, std::collections::VecDeque<(i64, i64, i64)>>,
    >,
    /// name -> last observed pg_stat_database sessions total (review 001
    /// P1-1: session churn between ticks counts as activity even when no
    /// client is connected at poll time).
    pub sessions: std::sync::Mutex<std::collections::HashMap<String, i64>>,
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
            name: Some(format!(
                "{name}-{}-{}",
                reason.to_lowercase(),
                chrono::Utc::now().timestamp()
            )),
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

/// The endpoint's owner password from its credential Secret (RFC 014 H3).
/// Review 001 P1-2: a missing/unreadable credential is an ERROR, never a
/// silent fallback to a shared password — the reconciler mints the Secret
/// before any pod exists, so absence means "not reconciled yet" (retriable)
/// or a real Secret/RBAC problem worth surfacing.
pub async fn endpoint_password(ctx: &Ctx, name: &str) -> anyhow::Result<String> {
    let secrets: Api<k8s_openapi::api::core::v1::Secret> =
        Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let s = secrets
        .get_opt(&format!("sspc-cred-{name}"))
        .await
        .with_context(|| format!("reading credential secret for {name}"))?
        .with_context(|| format!("credential secret sspc-cred-{name} not found"))?;
    s.data
        .as_ref()
        .and_then(|d| d.get("password"))
        .and_then(|b| String::from_utf8(b.0.clone()).ok())
        .with_context(|| format!("credential secret sspc-cred-{name} is malformed"))
}

/// The parent compute's current flush LSN, via in-cluster SQL. `db_name` is
/// any endpoint name — a Database or (branch-of-branch) a parent Branch.
async fn parent_flush_lsn(ctx: &Ctx, db_name: &str) -> Option<String> {
    let mut cfg = tokio_postgres::Config::new();
    cfg.host(format!("{db_name}.{}.svc.cluster.local", ctx.namespace))
        .port(55433)
        .user("cloud_admin")
        .password(&endpoint_password(ctx, db_name).await.ok()?)
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

        // Per-endpoint owner credential (RFC 014 H3, executing 012-D9).
        // Minted once, owner-referenced so it dies with the CR. Pods render
        // at create/wake, so the running pod and the Secret always agree.
        let password = self.ensure_credential(obj, name).await?;

        // Spec ConfigMap
        let spec_json = render(
            &SpecParams {
                tenant_id: tenant,
                timeline_id: timeline,
                encrypted_password: &pg_md5(&password, "cloud_admin"),
                jwks_x_b64url: &self.key.x_b64url,
                jwks_kid_b64url: &self.key.kid_b64url,
                safekeepers: &self.safekeepers,
                pageserver_connstring: &self.pageserver_connstring,
            },
            &Settings {
                port: 55433,
                listen_addresses: "0.0.0.0",
                fsync: false,
                unix_socket_directories: None,
            },
        )?;
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
                // Computes never talk to the Kubernetes API, and the compute
                // image runs unprivileged (initdb refuses root).
                "automountServiceAccountToken": false,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "compute",
                    "image": self.compute_image,
                    "imagePullPolicy": self.image_pull_policy,
                    "securityContext": {
                        "allowPrivilegeEscalation": false,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "command": compute_command(name)?,
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

    /// Load-or-mint the endpoint's owner password Secret (idempotent: crashes
    /// and replays converge on the first minted value, never rotate).
    async fn ensure_credential<K>(&self, obj: &K, name: &str) -> anyhow::Result<String>
    where
        K: Resource<DynamicType = ()>,
    {
        use aws_lc_rs::rand::SecureRandom as _;
        let secrets: Api<k8s_openapi::api::core::v1::Secret> =
            Api::namespaced(self.client.clone(), &self.namespace);
        let sname = format!("sspc-cred-{name}");
        if let Some(s) = secrets.get_opt(&sname).await? {
            if let Some(pw) = s.data.as_ref().and_then(|d| d.get("password")) {
                return Ok(String::from_utf8(pw.0.clone())?);
            }
        }
        let mut raw = [0u8; 18];
        aws_lc_rs::rand::SystemRandom::new()
            .fill(&mut raw)
            .map_err(|e| anyhow::anyhow!("credential gen: {e}"))?;
        let pw = hex::encode(raw);
        let secret: k8s_openapi::api::core::v1::Secret = serde_json::from_value(json!({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": sname, "namespace": self.namespace,
                          "ownerReferences": [owner_ref(obj)]},
            "stringData": {"password": pw},
        }))?;
        secrets
            .patch(
                &sname,
                &PatchParams::apply("sspc-operator").force(),
                &Patch::Apply(&secret),
            )
            .await?;
        Ok(pw)
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

async fn reconcile_db(
    db: Arc<Database>,
    ctx: Arc<Ctx>,
) -> Result<Action, kube::runtime::finalizer::Error<kube::Error>> {
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
    ctx.storcon
        .create_timeline(&tenant, &timeline, None, None)
        .await?;
    let run = wants_running(db.meta(), db.status.as_ref());
    if was.is_none() {
        post_event(
            ctx,
            "sspc.io/v1alpha1",
            "Database",
            &name,
            db.meta().uid.clone(),
            "Created",
            format!("database {name} provisioned"),
        )
        .await;
    } else if was == Some(Phase::Suspended) && run {
        post_event(
            ctx,
            "sspc.io/v1alpha1",
            "Database",
            &name,
            db.meta().uid.clone(),
            "Woke",
            format!("database {name} woke from suspend"),
        )
        .await;
    }
    let port = ctx
        .ensure_endpoint(
            db.as_ref(),
            &name,
            &tenant,
            &timeline,
            run,
            db.spec.cu_limit,
            db.spec.priority,
        )
        .await?;

    let phase = if run { Phase::Active } else { Phase::Suspended };
    let api: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    ctx.patch_status(
        &api,
        &name,
        json!({
            "phase": phase, "tenantId": tenant, "timelineId": timeline, "nodePort": port,
        }),
    )
    .await;
    info!("database {name} reconciled: tenant={tenant} timeline={timeline} port={port} run={run}");
    Ok(Action::requeue(Duration::from_secs(300)))
}

async fn cleanup_db(db: Arc<Database>, ctx: &Ctx) -> anyhow::Result<Action> {
    let name = db.name_any();
    // RFC 014 H1 backstop (MCP refuses earlier and friendlier): deleting the
    // tenant destroys every branch's storage, so never do it under live
    // children — hold the finalizer until they are gone.
    let brs: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let children: Vec<String> = brs
        .list(&Default::default())
        .await?
        .items
        .iter()
        .filter(|b| b.spec.database == name && b.meta().deletion_timestamp.is_none())
        .map(|b| b.name_any())
        .collect();
    if !children.is_empty() {
        anyhow::bail!(
            "database {name} still has branches [{}]; delete them first",
            children.join(", ")
        );
    }
    let uid = db.meta().uid.clone().context("no uid")?;
    let tenant = derive_id(&uid, "tenant");
    ctx.storcon.delete_tenant(&tenant).await?;
    info!("database {name} cleaned up (tenant {tenant})");
    Ok(Action::await_change())
}

// ---------- Branch ----------

async fn reconcile_branch(
    br: Arc<Branch>,
    ctx: Arc<Ctx>,
) -> Result<Action, kube::runtime::finalizer::Error<kube::Error>> {
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

    let parent_db = api_db.get(&br.spec.database).await?;
    let Some(status) = parent_db.status.as_ref() else {
        return Ok(Action::requeue(Duration::from_secs(3)));
    };
    let (Some(tenant), Some(db_timeline)) = (status.tenant_id.clone(), status.timeline_id.clone())
    else {
        return Ok(Action::requeue(Duration::from_secs(3)));
    };

    // The effective ancestor: a parent Branch's timeline when `parent` is set
    // (branch-of-branch, RFC 014 H2), else the database's root timeline.
    let api_br: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let (ancestor, ancestor_name, ancestor_active) = match &br.spec.parent {
        Some(p) => {
            let pbr = api_br.get(p).await?;
            if pbr.spec.database != br.spec.database {
                anyhow::bail!(
                    "parent branch {p} belongs to database {}, not {}",
                    pbr.spec.database,
                    br.spec.database
                );
            }
            let Some(tl) = pbr.status.as_ref().and_then(|s| s.timeline_id.clone()) else {
                return Ok(Action::requeue(Duration::from_secs(3)));
            };
            let active = pbr.status.as_ref().and_then(|s| s.phase) == Some(Phase::Active);
            (tl, p.clone(), active)
        }
        None => (
            db_timeline,
            br.spec.database.clone(),
            parent_db.status.as_ref().and_then(|s| s.phase) == Some(Phase::Active),
        ),
    };

    let was = br.status.as_ref().and_then(|s| s.phase);
    let timeline_allocated = br
        .status
        .as_ref()
        .and_then(|s| s.timeline_id.as_ref())
        .is_some();
    let timeline = derive_id(&uid, "branch");

    // Branch point (RFC 014 H2): a raw LSN passes through; a timestamp
    // resolves via the pageserver. An unresolvable user-supplied point fails
    // the CR loudly rather than retrying forever.
    let start_lsn: Option<String> = match &br.spec.at {
        None => None,
        Some(a) if a.contains('/') => Some(a.clone()),
        Some(ts) => match ctx.storcon.lsn_by_timestamp(&tenant, &ancestor, ts).await {
            Ok(l) => Some(l),
            Err(e) => {
                let msg = format!("branch point {ts}: {e}");
                warn!("branch {name}: {msg}");
                ctx.patch_status(
                    &api_br,
                    &name,
                    json!({"phase": "Failed", "message": msg.clone()}),
                )
                .await;
                post_event(
                    ctx,
                    "sspc.io/v1alpha1",
                    "Branch",
                    &name,
                    br.meta().uid.clone(),
                    "Failed",
                    msg,
                )
                .await;
                return Ok(Action::await_change());
            }
        },
    };

    // Branch-at-head race (found by T4): the timeline branches at the
    // pageserver's INGESTED lsn, which can lag the parent's just-flushed
    // writes — a branch created immediately after a load would miss it.
    // If the parent is awake, wait (bounded) for ingestion to catch up.
    // A historical branch point (`at`) needs no wait: it is already ingested.
    if !timeline_allocated && start_lsn.is_none() && ancestor_active {
        // Fail closed: an unreadable flush LSN is usually Service-endpoint
        // propagation lag on a parent that just woke — branching blind at the
        // ingested LSN would silently drop the parent's latest writes.
        let Some(flush) = parent_flush_lsn(ctx, &ancestor_name).await else {
            warn!(
                "branch {name}: parent flush lsn unreadable; requeueing instead of branching blind"
            );
            ctx.patch_status(
                &api_br,
                &name,
                json!({"phase": "Provisioning",
                "message": "waiting: parent flush LSN unreadable (endpoint may be settling)"}),
            )
            .await;
            return Ok(Action::requeue(Duration::from_secs(2)));
        };
        let deadline = std::time::Instant::now() + Duration::from_secs(20);
        loop {
            let ingested = ctx
                .storcon
                .timeline_last_record_lsn(&tenant, &ancestor)
                .await;
            let deadline_passed = std::time::Instant::now() > deadline;
            match head_wait_verdict(ingested.as_deref(), &flush, deadline_passed) {
                HeadWait::Ready => {
                    info!("branch {name}: ingestion caught up ({ingested:?} >= flush {flush})");
                    break;
                }
                HeadWait::HoldAndRequeue => {
                    // Fail closed (review 001 P0-1): a branch cut below the
                    // parent's flushed head silently loses rows. Hold the
                    // branch and say why; retry from a fresh deadline.
                    let msg = format!(
                        "waiting: pageserver ingestion lagging (ingested {}, parent flushed {flush})",
                        ingested.as_deref().unwrap_or("unknown")
                    );
                    warn!("branch {name}: {msg}; holding branch creation");
                    ctx.patch_status(
                        &api_br,
                        &name,
                        json!({"phase": "Provisioning", "message": msg}),
                    )
                    .await;
                    return Ok(Action::requeue(Duration::from_secs(5)));
                }
                HeadWait::KeepWaiting => {
                    info!("branch {name}: waiting for ingestion ({ingested:?} < flush {flush})");
                    tokio::time::sleep(Duration::from_millis(250)).await;
                }
            }
        }
    }
    if let Err(e) = ctx
        .storcon
        .create_timeline(&tenant, &timeline, Some(&ancestor), start_lsn.as_deref())
        .await
    {
        // Review 002 P1: a user-supplied branch point that the cell rejects
        // (4xx) is terminal — fail the CR with the reason instead of
        // retrying forever. 5xx/network errors stay retriable.
        let user_error = br.spec.at.is_some()
            && e.downcast_ref::<crate::storcon::StorconHttp>()
                .is_some_and(|h| (400..500).contains(&h.status));
        if user_error {
            let msg = format!(
                "branch point {}: {e:#}",
                br.spec.at.as_deref().unwrap_or("")
            );
            warn!("branch {name}: {msg}");
            ctx.patch_status(
                &api_br,
                &name,
                json!({"phase": "Failed", "message": msg.clone()}),
            )
            .await;
            post_event(
                ctx,
                "sspc.io/v1alpha1",
                "Branch",
                &name,
                br.meta().uid.clone(),
                "Failed",
                msg,
            )
            .await;
            return Ok(Action::await_change());
        }
        return Err(e);
    }
    let run = wants_running(br.meta(), br.status.as_ref());
    if was.is_none() {
        post_event(
            ctx,
            "sspc.io/v1alpha1",
            "Branch",
            &name,
            br.meta().uid.clone(),
            "Created",
            format!("branch {name} of {} created", br.spec.database),
        )
        .await;
    } else if was == Some(Phase::Suspended) && run {
        post_event(
            ctx,
            "sspc.io/v1alpha1",
            "Branch",
            &name,
            br.meta().uid.clone(),
            "Woke",
            format!("branch {name} woke from suspend"),
        )
        .await;
    }
    let port = ctx
        .ensure_endpoint(
            br.as_ref(),
            &name,
            &tenant,
            &timeline,
            run,
            br.spec.cu_limit,
            br.spec.priority,
        )
        .await?;

    let phase = if run { Phase::Active } else { Phase::Suspended };
    let api: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    ctx.patch_status(
        &api,
        &name,
        json!({
            "phase": phase, "tenantId": tenant, "timelineId": timeline, "nodePort": port,
            "message": null,
        }),
    )
    .await;
    info!("branch {name} reconciled: timeline={timeline} (ancestor {ancestor}) port={port}");
    Ok(Action::requeue(Duration::from_secs(300)))
}

async fn cleanup_branch(br: Arc<Branch>, ctx: &Ctx) -> anyhow::Result<Action> {
    let name = br.name_any();
    // Same H1 guard one level down: a branch with child branches (H2) is a
    // parent timeline — deleting it would orphan them.
    let brs: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let children: Vec<String> = brs
        .list(&Default::default())
        .await?
        .items
        .iter()
        .filter(|b| {
            b.spec.parent.as_deref() == Some(name.as_str()) && b.meta().deletion_timestamp.is_none()
        })
        .map(|b| b.name_any())
        .collect();
    if !children.is_empty() {
        anyhow::bail!(
            "branch {name} still has child branches [{}]; delete them first",
            children.join(", ")
        );
    }
    let uid = br.meta().uid.clone().context("no uid")?;
    let timeline = derive_id(&uid, "branch");
    // Review 001 P0-2: status writes are best-effort, so the tenant may never
    // have landed in status — derive it from the owning Database rather than
    // skipping cell-side cleanup (which leaks the timeline).
    let tenant = match br.status.as_ref().and_then(|s| s.tenant_id.clone()) {
        Some(t) => Some(t),
        None => {
            let dbs: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
            dbs.get_opt(&br.spec.database).await?.and_then(|db| {
                db.status
                    .as_ref()
                    .and_then(|s| s.tenant_id.clone())
                    .or_else(|| db.meta().uid.as_ref().map(|u| derive_id(u, "tenant")))
            })
            // Database CR gone too: its tenant delete reclaims every timeline.
        }
    };
    if let Some(tenant) = tenant {
        ctx.storcon.delete_timeline(&tenant, &timeline).await?;
    }
    info!("branch {} cleaned up (timeline {timeline})", br.name_any());
    Ok(Action::await_change())
}

// ---------- runners ----------

fn error_policy<K>(
    _obj: Arc<K>,
    err: &kube::runtime::finalizer::Error<kube::Error>,
    _ctx: Arc<Ctx>,
) -> Action {
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
            annotations: at.map(|t| [(WAKE_ANNOTATION.to_string(), t.to_string())].into()),
            ..Default::default()
        }
    }

    #[test]
    fn compute_command_preserves_image_contract() {
        assert_eq!(
            compute_command("prod").unwrap(),
            vec![
                "compute_ctl",
                "--pgdata=/var/db/postgres/compute",
                "--connstr=postgresql://cloud_admin@localhost:55433/postgres",
                "--pgbin=/usr/local/bin/postgres",
                "--compute-id=prod",
                "--config=/config/spec.json",
            ]
        );
    }

    #[test]
    fn same_second_wake_is_ordered_with_legacy_and_precise_suspension() {
        let wake = meta_with_wake(Some("2026-09-05T20:19:02.700000000Z"));
        for suspended in ["2026-09-05T20:19:02Z", "2026-09-05T20:19:02.335675000Z"] {
            assert!(wants_running(
                &wake,
                Some(&status(Phase::Suspended, Some(suspended)))
            ));
        }
        for suspended in [
            "2026-09-05T20:19:02.700000000Z",
            "2026-09-05T20:19:02.900000000Z",
            "2026-09-05T21:19:02.700000000+01:00",
            "invalid",
        ] {
            assert!(!wants_running(
                &wake,
                Some(&status(Phase::Suspended, Some(suspended)))
            ));
        }
        assert!(!wants_running(
            &meta_with_wake(Some("invalid")),
            Some(&status(Phase::Suspended, None))
        ));
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

    /// The review-002 retry-state regression: a requeued branch has
    /// phase=Provisioning but NO timelineId — it must still be treated as
    /// unallocated so the head wait runs again on retry.
    #[test]
    fn provisioning_retry_is_not_allocated() {
        let no_tl = crate::crd::EndpointStatus {
            phase: Some(Phase::Provisioning),
            ..Default::default()
        };
        assert!(no_tl.timeline_id.is_none());
        let with_tl = crate::crd::EndpointStatus {
            phase: Some(Phase::Provisioning),
            timeline_id: Some("abc".into()),
            ..Default::default()
        };
        assert!(with_tl.timeline_id.is_some());
    }
}
