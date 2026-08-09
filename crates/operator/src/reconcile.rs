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

use crate::crd::{Branch, Database, Phase};
use crate::keys::ComputeKey;
use crate::ports;
use crate::spec::{SpecParams, render};
use crate::storcon::Storcon;

pub const FINALIZER: &str = "sspc.io/cell-cleanup";
pub const ENDPOINT_LABEL: &str = "sspc.io/endpoint";

pub struct Ctx {
    pub client: Client,
    pub storcon: Storcon,
    pub key: ComputeKey,
    pub namespace: String,
    pub compute_image: String,
    pub image_pull_policy: String,
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
    async fn ensure_endpoint<K>(
        &self,
        obj: &K,
        name: &str,
        tenant: &str,
        timeline: &str,
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

        // Compute pod: compute_ctl as PID 1 (D6), stock image, spec from the CM.
        let pod: Pod = serde_json::from_value(json!({
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": ns,
                          "labels": {"app": "compute", "sspc.io/compute": name},
                          "ownerReferences": [oref]},
            "spec": {
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
                    "ports": [{"containerPort": 55433}, {"containerPort": 3080}],
                    "volumeMounts": [{"name": "spec", "mountPath": "/config"}],
                    "readinessProbe": {"tcpSocket": {"port": 55433}, "periodSeconds": 1},
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

    ctx.storcon.create_tenant(&tenant).await?;
    ctx.storcon.create_timeline(&tenant, &timeline, None).await?;
    let port = ctx.ensure_endpoint(db.as_ref(), &name, &tenant, &timeline).await?;

    let api: Api<Database> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    ctx.patch_status(&api, &name, json!({
        "phase": Phase::Active, "tenantId": tenant, "timelineId": timeline, "nodePort": port,
    }))
    .await;
    info!("database {name} reconciled: tenant={tenant} timeline={timeline} port={port}");
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

    let timeline = derive_id(&uid, "branch");
    ctx.storcon
        .create_timeline(&tenant, &timeline, Some(&ancestor))
        .await?;
    let port = ctx.ensure_endpoint(br.as_ref(), &name, &tenant, &timeline).await?;

    let api: Api<Branch> = Api::namespaced(ctx.client.clone(), &ctx.namespace);
    ctx.patch_status(&api, &name, json!({
        "phase": Phase::Active, "tenantId": tenant, "timelineId": timeline, "nodePort": port,
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
