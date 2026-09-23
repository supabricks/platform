//! Resolve one committed dataset set, then hand Sail a credential-free frozen adapter.
//! Active analytical_sessions rows are the durable publication/snapshot lease (UC03).
use super::{
    Manager,
    adapter::Adapter,
    publication::{self, Publication},
};
use crate::store::{AnalyticalSession, Result, Store, error::conflict};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    thread::JoinHandle,
    time::{Duration, Instant},
};
use supabricks_core::resource::{DeploymentId, OperationId};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Read {
    pub deployment_id: DeploymentId,
    pub publication_id: OperationId,
    pub revision: i64,
    pub validated: bool,
}
type Outcome = std::result::Result<(), &'static str>;
#[derive(Default)]
pub(crate) struct Pending {
    workers: BTreeMap<String, JoinHandle<Outcome>>,
}
fn publication(store: &Store, s: &AnalyticalSession) -> Result<Publication> {
    let read = s.catalog.as_ref().unwrap();
    let p = store.catalog_publication(read.deployment_id, read.publication_id)?;
    // Retirement may withdraw the head, but cannot delete a leased revision.
    if !matches!(p.state.as_str(), "published" | "retiring")
        || p.revision != Some(read.revision)
        || p.project_id != s.project_id
        || p.branch_id != s.branch_id
        || Some(p.epoch_id) != s.epoch_id
    {
        return Err(conflict("catalog session publication identity changed"));
    }
    publication::verify_locations(store, &p)?;
    Ok(p)
}
pub(crate) fn adapter(store: &Store, manager: &Manager, p: &Publication) -> Result<Adapter> {
    let a = manager.adapter(store)?;
    if manager.status()["mode"] != "local"
        || a.provider_id != p.namespace.provider_id
        || a.probe.expected_metastore.as_ref() != Some(&p.namespace.metastore_id)
    {
        return Err(conflict("catalog session provider identity changed"));
    }
    Ok(a)
}
pub(crate) fn validate(a: &Adapter, p: &Publication) -> Outcome {
    if p.namespace.catalog_id.is_none() || p.namespace.schema_id.is_none() {
        return Err("catalog namespace has no recorded UUIDs");
    }
    let started = Instant::now();
    a.namespace(p.namespace.clone())
        .map_err(|_| "catalog namespace validation failed")?;
    for table in &p.tables {
        if started.elapsed() > Duration::from_secs(15) {
            return Err("catalog resolution deadline exceeded");
        }
        let path = format!(
            "tables/{}.{}.{}",
            p.namespace.catalog,
            p.namespace.schema,
            table.body["name"]
                .as_str()
                .ok_or("catalog table name missing")?
        );
        let remote = a
            .owned_request("GET", &path, None, None)
            .map_err(|_| "catalog table resolution failed")?;
        publication::worker::identity(&remote, table)
            .map_err(|_| "catalog table UUID, schema or location changed")?;
    }
    Ok(())
}
impl Pending {
    pub fn reap(&mut self, store: &Store) -> Result<()> {
        let active = store.active_analytical_sessions()?;
        self.workers
            .retain(|id, w| !w.is_finished() || active.iter().any(|s| s.id.to_string() == *id));
        Ok(())
    }
    pub fn ready(
        &mut self,
        store: &mut Store,
        manager: &Manager,
        s: &mut AnalyticalSession,
    ) -> Result<bool> {
        if s.catalog.as_ref().is_none_or(|r| r.validated)
            && (s.datasets.is_empty() || s.datasets_validated)
        {
            return Ok(true);
        }
        let id = s.id.to_string();
        if let Some(worker) = self.workers.get(&id) {
            if !worker.is_finished() {
                return Ok(false);
            }
            self.workers
                .remove(&id)
                .unwrap()
                .join()
                .unwrap_or(Err("catalog resolution worker failed"))
                .map_err(conflict)?;
            // A provider switch while resolving must never authorize a stale result.
            inputs(store, manager, s)?;
            if let Some(read) = &mut s.catalog {
                read.validated = true;
            }
            s.datasets_validated = true;
            store.save_analytical_session(s)?;
            return Ok(true);
        }
        if self.workers.len() >= 2 {
            return Err(conflict("catalog resolution slots occupied"));
        }
        let inputs = inputs(store, manager, s)?;
        self.workers.insert(
            id,
            std::thread::spawn(move || {
                let start = Instant::now();
                for (a, p) in inputs {
                    if start.elapsed() > Duration::from_secs(30) {
                        return Err("catalog dataset resolution deadline exceeded");
                    }
                    validate(&a, &p)?;
                }
                Ok(())
            }),
        );
        Ok(false)
    }
}
/// No remote names are consulted after this descriptor is installed. UC credentials
/// stay in the daemon; local-owner file access expires with the session's worker lease.
pub(crate) fn frozen(store: &Store, s: &AnalyticalSession) -> Result<Option<Value>> {
    let Some(read) = &s.catalog else {
        return Ok(None);
    };
    if !read.validated {
        return Err(conflict("catalog session has not been validated"));
    }
    let p = publication(store, s)?;
    Ok(Some(frozen_publication(&p, read.revision)))
}
fn frozen_publication(p: &Publication, revision: i64) -> Value {
    let tables: Vec<Value> = p
        .tables
        .iter()
        .map(|t| {
            json!({
                "table_id":t.id,"schema":t.source_schema,"name":t.source_name,
                "uc_schema":p.namespace.schema,"uc_name":t.body["name"],"delta_version":0,
            })
        })
        .collect();
    json!({"catalog":p.namespace.catalog,"tables":tables,
        "provenance":{"provider":"oss_unity_catalog","adapter":"frozen_local_delta",
        "provider_id":p.namespace.provider_id,"metastore_id":p.namespace.metastore_id,
        "catalog_id":p.namespace.catalog_id,"schema_id":p.namespace.schema_id,
        "catalog":p.namespace.catalog,"publication_id":p.id,"revision":revision,"snapshot_at_ms":p.snapshot_at_ms,"source_branch_id":p.branch_id,
        "tables":tables}})
}

fn inputs(
    store: &Store,
    manager: &Manager,
    s: &AnalyticalSession,
) -> Result<Vec<(Adapter, Publication)>> {
    let mut publications = BTreeMap::new();
    if s.catalog.is_some() {
        let p = publication(store, s)?;
        publications.insert(p.id.to_string(), p);
    }
    for d in s.datasets.values() {
        let p = super::datasets::verify(store, d, false)?;
        publications.insert(p.id.to_string(), p);
    }
    publications
        .into_values()
        .map(|p| Ok((adapter(store, manager, &p)?, p)))
        .collect()
}
pub(crate) fn frozen_datasets(store: &Store, s: &AnalyticalSession) -> Result<Vec<Value>> {
    if !s.datasets.is_empty() && !s.datasets_validated {
        return Err(conflict("dataset inputs are not validated"));
    }
    s.datasets
        .iter()
        .map(|(logical, d)| {
            let p = super::datasets::verify(store, d, false)?;
            let mut v = frozen_publication(&p, d.revision);
            let alias = format!(
                "dataset_{}",
                logical
                    .strip_prefix("dataset.")
                    .ok_or_else(|| conflict("invalid dataset logical key"))?
            );
            v["catalog"] = json!(alias);
            v["provenance"]["binding"] = json!(logical);
            v["provenance"]["alias"] = json!(alias);
            v["provenance"]["owner_deployment_id"] = json!(d.target.deployment_id);
            v["provenance"]["epoch_id"] = json!(d.epoch_id);
            v["provenance"]["schema_sha256"] = json!(d.schema_sha256);
            v["provenance"]["content_sha256"] = json!(d.content_sha256);
            let snapshot = store.snapshot(p.project_id, p.epoch_id)?;
            let descriptor = snapshot
                .publication
                .descriptor
                .ok_or_else(|| conflict("dataset descriptor missing"))?;
            v["descriptor"] =
                publication::read_view(store, snapshot.publication.export_id, &descriptor)?.1;
            Ok(v)
        })
        .collect()
}
