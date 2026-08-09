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

use crate::crd::{Branch, Database, EndpointStatus, Phase};
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
    let dbs: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    for db in dbs.list(&Default::default()).await?.items {
        let name = db.name_any();
        if ttl_expired(db.meta(), db.spec.ttl_seconds) {
            reap(ctx, &dbs, &db, "Database", db.spec.ttl_seconds.unwrap_or(0)).await;
            continue;
        }
        maybe_suspend::<Database>(ctx, &name, db.spec.suspend_after_seconds, db.status.as_ref())
            .await;
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
    }
    Ok(())
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
        .password(&ctx.pg_password)
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
