//! The lifecycle loop (RFC 012 P3): idle-suspend and TTL reaping.
//!
//! Day-2 verdicts baked in: compute_ctl will not suspend itself and its
//! `last_active` is unreliable, so the operator polls Postgres directly
//! (M1's activity truth; the gateway takes over in M2). Suspend is the
//! verified sequence: authed `POST /terminate` → record flush LSN → delete
//! pod. The Service stays — the port is sticky through suspension.

use std::sync::Arc;
use std::time::Duration;

use k8s_openapi::api::core::v1::{Event, Pod};
use kube::api::{Api, DeleteParams, ObjectMeta, Patch, PatchParams, PostParams};
use kube::{Resource, ResourceExt};
use serde_json::json;
use tracing::{info, warn};

use crate::crd::{Branch, Database, EndpointStatus, EnrolledDatabase, EnrolledPhase, Phase};
use crate::reconcile::{Ctx, now_ts};

const TICK: Duration = Duration::from_secs(15);

pub async fn run(ctx: Arc<Ctx>) {
    loop {
        if let Err(e) = tick(&ctx).await {
            warn!("lifecycle tick failed: {e:#}");
        }
        tokio::time::sleep(TICK).await;
    }
}

async fn tick(ctx: &Ctx) -> anyhow::Result<()> {
    let mut endpoint_names: Vec<String> = Vec::new();
    let dbs: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    for db in dbs.list(&Default::default()).await?.items {
        let name = db.name_any();
        if ttl_expired(db.meta(), db.spec.ttl_seconds) {
            reap(ctx, &dbs, &db, "Database", db.spec.ttl_seconds.unwrap_or(0)).await;
            continue;
        }
        maybe_suspend::<Database>(ctx, &name, db.spec.suspend_after_seconds, db.status.as_ref())
            .await;
        endpoint_names.push(name);
    }
    let brs: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    for br in brs.list(&Default::default()).await?.items {
        let name = br.name_any();
        if ttl_expired(br.meta(), br.spec.ttl_seconds) {
            reap(ctx, &brs, &br, "Branch", br.spec.ttl_seconds.unwrap_or(0)).await;
            continue;
        }
        maybe_suspend::<Branch>(ctx, &name, br.spec.suspend_after_seconds, br.status.as_ref())
            .await;
        endpoint_names.push(name);
    }
    let edbs: Api<EnrolledDatabase> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    for edb in edbs.list(&Default::default()).await?.items {
        let first = edb.status.as_ref().and_then(|s| s.phase).is_none();
        check_enrolled(&edbs, &edb).await;
        if first {
            crate::reconcile::post_event(
                ctx, "sspc.io/v1alpha1", "EnrolledDatabase", &edb.name_any(),
                edb.meta().uid.clone(), "Enrolled",
                format!("{} enrolled — inventoried in place, nothing migrated", edb.name_any()),
            )
            .await;
        }
    }
    sample_metrics(ctx, &endpoint_names).await;
    Ok(())
}

/// Agentless SQL health for an enrolled (foreign) Postgres — RFC 010:
/// observe and advise, never operate. One round-trip, read-only.
async fn check_enrolled(api: &Api<EnrolledDatabase>, edb: &EnrolledDatabase) {
    let name = edb.name_any();
    let status = match probe_enrolled(&edb.spec.connection_uri).await {
        Ok((version, dbs, size)) => json!({
            "phase": EnrolledPhase::Reachable, "serverVersion": version,
            "databaseCount": dbs, "totalSize": size,
            "lastChecked": now_ts(), "message": null,
        }),
        Err(e) => json!({
            "phase": EnrolledPhase::Unreachable,
            "lastChecked": now_ts(), "message": e.to_string(),
        }),
    };
    let _ = api
        .patch_status(&name, &PatchParams::default(), &Patch::Merge(json!({"status": status})))
        .await;
}

async fn probe_enrolled(uri: &str) -> anyhow::Result<(String, i64, String)> {
    let mut cfg: tokio_postgres::Config = uri.parse()?;
    cfg.application_name("sspc-operator")
        .connect_timeout(Duration::from_secs(3));
    let (client, conn) = cfg.connect(tokio_postgres::NoTls).await?;
    let handle = tokio::spawn(conn);
    let row = client
        .query_one(
            "SELECT current_setting('server_version'), \
             (SELECT count(*) FROM pg_database WHERE NOT datistemplate), \
             (SELECT pg_size_pretty(sum(pg_database_size(oid))::bigint) \
              FROM pg_database WHERE NOT datistemplate)",
            &[],
        )
        .await?;
    let out = (row.get::<_, String>(0), row.get::<_, i64>(1), row.get::<_, String>(2));
    drop(client);
    handle.abort();
    Ok(out)
}

/// Basic usage sampling (013 round 2): kubelet Summary API via the node
/// proxy — per-pod CPU nanocores and working-set bytes, no metrics-server.
async fn sample_metrics(ctx: &Ctx, names: &[String]) {
    let nodes: Api<k8s_openapi::api::core::v1::Node> = Api::all(ctx.client.clone());
    let Ok(list) = nodes.list(&Default::default()).await else { return };
    let now = chrono::Utc::now().timestamp();
    let mut samples: Vec<(String, i64, i64)> = Vec::new();
    for node in list.items {
        let node_name = node.name_any();
        let req = match http::Request::get(format!("/api/v1/nodes/{node_name}/proxy/stats/summary"))
            .body(Vec::new())
        {
            Ok(r) => r,
            Err(_) => continue,
        };
        let v = match ctx.client.request::<serde_json::Value>(req).await {
            Ok(v) => v,
            Err(e) => {
                warn!("kubelet summary for {node_name}: {e}");
                continue;
            }
        };
        let empty = Vec::new();
        for pod in v["pods"].as_array().unwrap_or(&empty) {
            if pod["podRef"]["namespace"].as_str() != Some(ctx.namespace.as_str()) {
                continue;
            }
            let Some(pname) = pod["podRef"]["name"].as_str() else { continue };
            if !names.iter().any(|n| n == pname) {
                continue;
            }
            let cpu_m = pod["cpu"]["usageNanoCores"].as_u64().unwrap_or(0) / 1_000_000;
            let mem_mi = pod["memory"]["workingSetBytes"].as_u64().unwrap_or(0) / (1024 * 1024);
            samples.push((pname.to_string(), cpu_m as i64, mem_mi as i64));
        }
    }
    let mut m = ctx.metrics.lock().unwrap();
    for (name, cpu, mem) in samples {
        let ring = m.entry(name).or_default();
        ring.push_back((now, cpu, mem));
        while ring.len() > 40 {
            ring.pop_front();
        }
    }
    m.retain(|k, _| names.iter().any(|n| n == k));
}

fn ttl_expired(meta: &ObjectMeta, ttl_seconds: Option<i64>) -> bool {
    let (Some(ttl), Some(created)) = (ttl_seconds, meta.creation_timestamp.as_ref()) else {
        return false;
    };
    // k8s-openapi Time is a jiff Timestamp; compare in unix seconds.
    chrono::Utc::now().timestamp() - created.0.as_second() >= ttl
}

/// TTL reap: audit Event on the resource, then delete (finalizer + ownerRef
/// GC do the actual teardown). The Event is demo step 5's "audit line".
async fn reap<K>(ctx: &Ctx, api: &Api<K>, obj: &K, kind: &str, ttl: i64)
where
    K: Resource<DynamicType = ()> + Clone + serde::de::DeserializeOwned + std::fmt::Debug,
{
    let name = obj.name_any();
    let ev = Event {
        metadata: ObjectMeta {
            name: Some(format!(
                "{name}-ttl-{}",
                chrono::Utc::now().timestamp()
            )),
            namespace: Some(ctx.namespace.clone()),
            ..Default::default()
        },
        type_: Some("Normal".into()),
        reason: Some("TTLExpired".into()),
        action: Some("TTLReap".into()),
        message: Some(format!("{kind} {name}: TTL of {ttl}s expired; reaping")),
        involved_object: k8s_openapi::api::core::v1::ObjectReference {
            api_version: Some(K::api_version(&()).into_owned()),
            kind: Some(K::kind(&()).into_owned()),
            name: Some(name.clone()),
            namespace: Some(ctx.namespace.clone()),
            uid: obj.meta().uid.clone(),
            ..Default::default()
        },
        ..Default::default()
    };
    let events: Api<Event> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    if let Err(e) = events.create(&PostParams::default(), &ev).await {
        warn!("ttl event for {name}: {e}");
    }
    match api.delete(&name, &DeleteParams::default()).await {
        Ok(_) => info!("TTL reaped {kind} {name} (ttl {ttl}s)"),
        Err(e) => warn!("ttl delete {name}: {e}"),
    }
}

/// Idle detection + suspend for one Active endpoint.
async fn maybe_suspend<K>(
    ctx: &Ctx,
    name: &str,
    suspend_after: i64,
    status: Option<&EndpointStatus>,
) where
    K: Resource<DynamicType = (), Scope = k8s_openapi::NamespaceResourceScope>
        + Clone
        + serde::de::DeserializeOwned
        + std::fmt::Debug,
{
    if suspend_after <= 0 || status.map(|s| s.phase) != Some(Some(Phase::Active)) {
        return;
    }
    let pods: Api<Pod> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    let Ok(Some(pod)) = pods.get_opt(name).await else {
        return; // no pod: nothing to suspend (reconciler owns convergence)
    };
    let Some(pod_ip) = pod.status.as_ref().and_then(|s| s.pod_ip.clone()) else {
        return;
    };

    let api: Api<K> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    match check_activity(ctx, name).await {
        None => (), // unreachable/starting — don't decide anything this tick
        Some(true) => {
            let _ = api
                .patch_status(
                    name,
                    &PatchParams::default(),
                    &Patch::Merge(json!({"status": {"lastActivity": now_ts()}})),
                )
                .await;
        }
        Some(false) => {
            // Idle since lastActivity, or since pod start if never seen active.
            let idle_since = status
                .and_then(|s| s.last_activity.clone())
                .or_else(|| {
                    pod.status
                        .as_ref()
                        .and_then(|s| s.start_time.as_ref())
                        .map(|t| t.0.to_string()) // jiff Display = RFC3339
                });
            let Some(since) = idle_since
                .and_then(|s| chrono::DateTime::parse_from_rfc3339(&s).ok())
            else {
                return;
            };
            let idle_secs = (chrono::Utc::now()
                - since.with_timezone(&chrono::Utc))
            .num_seconds();
            if idle_secs < suspend_after {
                return;
            }
            let lsn = terminate_compute(ctx, &pod_ip).await;
            let _ = pods.delete(name, &DeleteParams::default()).await;
            crate::reconcile::post_event(
                ctx, "sspc.io/v1alpha1", K::kind(&()).as_ref(), name, None,
                "Suspended",
                format!("{name} suspended after {idle_secs}s idle — compute released"),
            )
            .await;
            let mut st = json!({"phase": Phase::Suspended, "suspendedAt": now_ts()});
            if let Some(l) = lsn {
                st["flushLsn"] = json!(l);
            }
            let _ = api
                .patch_status(name, &PatchParams::default(), &Patch::Merge(json!({"status": st})))
                .await;
            info!("suspended {name} after {idle_secs}s idle (threshold {suspend_after}s)");
        }
    }
}

/// True = client backends (besides ours) exist. None = couldn't tell.
async fn check_activity(ctx: &Ctx, name: &str) -> Option<bool> {
    let mut cfg = tokio_postgres::Config::new();
    cfg.host(format!("{name}.{}.svc.cluster.local", ctx.namespace))
        .port(55433)
        .user("cloud_admin")
        .password(&crate::reconcile::endpoint_password(ctx, name).await)
        .dbname("postgres")
        .application_name("sspc-operator")
        .connect_timeout(Duration::from_secs(3));
    let (client, conn) = cfg.connect(tokio_postgres::NoTls).await.ok()?;
    let handle = tokio::spawn(conn);
    let row = client
        .query_one(
            // Excludes ourselves AND compute_ctl's internal connections
            // (compute_ctl:compute_monitor holds a persistent session — the
            // local reproduction of the research doc's check_availability
            // "never truly zero" trap).
            "SELECT count(*)::int4 FROM pg_stat_activity \
             WHERE backend_type = 'client backend' \
               AND application_name <> 'sspc-operator' \
               AND application_name NOT LIKE 'compute_ctl%'",
            &[],
        )
        .await
        .ok()?;
    let active: i32 = row.get(0);
    drop(client);
    handle.abort();
    Some(active > 0)
}

/// Day-2 suspend sequence: authed POST /terminate, return the flush LSN.
async fn terminate_compute(ctx: &Ctx, pod_ip: &str) -> Option<String> {
    let token = ctx.key.mint_admin_jwt(300).ok()?;
    let resp = reqwest::Client::new()
        .post(format!("http://{pod_ip}:3080/terminate"))
        .bearer_auth(token)
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .ok()?;
    let v: serde_json::Value = resp.json().await.ok()?;
    v["lsn"].as_str().map(String::from)
}
