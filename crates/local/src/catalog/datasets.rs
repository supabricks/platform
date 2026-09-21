//! UC05 destination-owned, fixed publication bindings. Packages carry requirements,
//! never destination authority. All mutable mapping changes use project plan/apply.
use super::{
    Manager,
    publication::{self, Publication},
};
use crate::{
    api::Binding,
    project_apply::{self, Resource},
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    thread::JoinHandle,
    time::{Duration, Instant},
};
use supabricks_core::resource::{BranchId, DeploymentId, EpochId, OperationId};

#[cfg(test)]
mod tests;

pub const CAPABILITY: &str = "catalog-datasets-v1";
pub const MAX_DATASETS: usize = 8;
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub deployment_id: DeploymentId,
    pub provider_id: String,
    pub publication_id: OperationId,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Dataset {
    pub target: Target,
    pub epoch_id: EpochId,
    pub branch_id: BranchId,
    pub revision: i64,
    pub schema_sha256: String,
    pub content_sha256: String,
    pub snapshot_at_ms: Option<i64>,
    pub catalog: String,
    pub tables: Vec<Value>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    List,
    References { target: Target },
    Describe { target: Target },
    Updates { logical: String },
}
pub fn describe(store: &Store, target: &Target, admitting: bool) -> Result<Dataset> {
    let p = store.catalog_publication(target.deployment_id, target.publication_id)?;
    if p.namespace.provider_id != target.provider_id
        || p.revision.is_none()
        || !(p.state == "published" || (!admitting && p.state == "retiring"))
    {
        return Err(conflict(
            "catalog dataset publication withdrawn or provider identity changed",
        ));
    }
    publication::verify_locations(store, &p)?;
    let tables = logical_tables(&p);
    Ok(Dataset {
        target: target.clone(),
        epoch_id: p.epoch_id,
        branch_id: p.branch_id,
        revision: p.revision.unwrap(),
        schema_sha256: project_apply::digest(&tables)?,
        content_sha256: p.manifest_hash,
        snapshot_at_ms: p.snapshot_at_ms,
        catalog: p.namespace.catalog,
        tables,
    })
}
// Physical table/OID ordering can differ in an otherwise compatible destination.
fn logical_tables(p: &Publication) -> Vec<Value> {
    let mut tables: Vec<Value> = p
        .tables
        .iter()
        .map(|t| json!({"schema":t.source_schema,"name":t.source_name,"columns":t.body["columns"]}))
        .collect();
    tables.sort_by(|a, b| {
        (a["schema"].as_str(), a["name"].as_str()).cmp(&(b["schema"].as_str(), b["name"].as_str()))
    });
    tables
}
pub(crate) fn from_resource(r: &Resource) -> Result<Dataset> {
    if r.kind != "catalog_dataset" {
        return Err(invalid("resource is not a catalog dataset"));
    }
    Ok(serde_json::from_value(
        r.receipt
            .clone()
            .ok_or_else(|| conflict("dataset receipt missing"))?,
    )?)
}
pub(crate) fn installed(
    store: &Store,
    deployment: DeploymentId,
) -> Result<BTreeMap<String, Dataset>> {
    store
        .deployment_resources(deployment)?
        .into_iter()
        .filter(|(_, r)| r.kind == "catalog_dataset")
        .map(|(k, r)| Ok((k, from_resource(&r)?)))
        .collect()
}
pub(crate) fn verify(store: &Store, d: &Dataset, admitting: bool) -> Result<Publication> {
    if describe(store, &d.target, admitting)? != *d {
        return Err(conflict(
            "dataset schema/content/provenance changed; review a new plan",
        ));
    }
    store.catalog_publication(d.target.deployment_id, d.target.publication_id)
}
pub(crate) fn selected(step: &project_apply::Step) -> Result<Dataset> {
    Ok(serde_json::from_value(
        step.initialization
            .as_ref()
            .and_then(|v| v.get("selected"))
            .cloned()
            .ok_or_else(|| {
                conflict(
                    "dataset requirement is unresolved; supply an explicit destination mapping",
                )
            })?,
    )?)
}
pub fn handle(store: &Store, binding: &Binding, command: Command) -> Result<Value> {
    let owner = store.binding_context(binding)?;
    let bindings = installed(store, owner.deployment_id)?;
    match command {
        Command::References { target } => {
            if owner.deployment_id != target.deployment_id {
                return Err(conflict(
                    "retention inspection requires the producer project",
                ));
            }
            let p = store.catalog_publication(target.deployment_id, target.publication_id)?;
            if p.namespace.provider_id != target.provider_id {
                return Err(conflict("provider identity changed"));
            }
            Ok(
                json!({"api_version":1,"publication_id":p.id,"state":p.state,"references":store.dataset_references(p.id)?,"holders":store.dataset_holders(p.id)?,"other_references":store.dataset_other_references(p.epoch_id)?}),
            )
        }
        Command::Describe { target } => {
            Ok(json!({"api_version":1,"dataset":describe(store,&target,true)?}))
        }
        Command::List => {
            let values: Vec<Value> = bindings
                .into_iter()
                .map(|(logical, d)| {
                    let state = store
                        .catalog_publication(d.target.deployment_id, d.target.publication_id)
                        .map(|p| p.state)
                        .unwrap_or_else(|_| "unavailable".into());
                    json!({"logical":logical,"dataset":d,"state":state,"retained":true})
                })
                .collect();
            Ok(
                json!({"api_version":1,"deployment_id":owner.deployment_id,"bindings":values,"limit":MAX_DATASETS}),
            )
        }
        Command::Updates { logical } => {
            let current = bindings
                .get(&logical)
                .ok_or_else(|| invalid("dataset binding not found in project"))?;
            let (_, head) = store.catalog_head(current.target.deployment_id, current.branch_id)?;
            let newer = head
                .map(|publication_id| {
                    describe(
                        store,
                        &Target {
                            publication_id,
                            ..current.target.clone()
                        },
                        true,
                    )
                })
                .transpose()?;
            Ok(
                json!({"api_version":1,"logical":logical,"current":current,"available":newer,
                "schema_changed":newer.as_ref().is_some_and(|n|n.schema_sha256!=current.schema_sha256),
                "content_changed":newer.as_ref().is_some_and(|n|n.content_sha256!=current.content_sha256),
                "adopted":false}),
            )
        }
    }
}
/// Provider I/O never blocks the single state writer or performs catalog mutations.
#[derive(Default)]
pub struct Checks {
    tasks: BTreeMap<String, JoinHandle<std::result::Result<(), &'static str>>>,
}
impl Checks {
    pub fn reap(&mut self, store: &Store) -> Result<()> {
        let pending = store.pending_applies()?;
        self.tasks
            .retain(|id, t| !t.is_finished() || pending.iter().any(|p| p.id.to_string() == *id));
        Ok(())
    }
    pub fn ready(
        &mut self,
        store: &Store,
        manager: Option<&Manager>,
        op: &project_apply::Operation,
    ) -> Result<bool> {
        let id = op.id.to_string();
        if self.tasks.get(&id).is_some_and(|task| !task.is_finished()) {
            return Ok(false);
        }
        let datasets: Vec<Dataset> = op
            .plan
            .steps
            .iter()
            .filter(|s| s.kind == "catalog_dataset" && s.action != "unbind")
            .map(selected)
            .collect::<Result<_>>()?;
        if datasets.is_empty() {
            return Ok(true);
        }
        let manager = manager
            .ok_or_else(|| conflict("dataset activation requires a ready catalog provider"))?;
        let mut inputs = vec![];
        for d in datasets {
            let p = verify(store, &d, true)?;
            let adapter = super::reads::adapter(store, manager, &p)?;
            inputs.push((adapter, p));
        }
        if let Some(t) = self.tasks.get(&id) {
            if !t.is_finished() {
                return Ok(false);
            }
            self.tasks
                .remove(&id)
                .unwrap()
                .join()
                .unwrap_or(Err("dataset validation worker failed"))
                .map_err(conflict)?;
            return Ok(true);
        }
        if self.tasks.len() >= 2 {
            return Ok(false);
        }
        self.tasks.insert(
            id,
            std::thread::spawn(move || {
                let started = Instant::now();
                for (a, p) in inputs {
                    if started.elapsed() > Duration::from_secs(30) {
                        return Err("dataset validation deadline exceeded");
                    }
                    super::reads::validate(&a, &p)?;
                }
                Ok(())
            }),
        );
        Ok(false)
    }
}
