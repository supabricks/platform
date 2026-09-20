use super::*;
use crate::{
    api::Binding,
    catalog::{Manager, adapter::Adapter},
    deployments::Context,
    store::{
        Store,
        error::{conflict, missing},
    },
};
use std::{
    collections::HashMap,
    thread::JoinHandle,
    time::{Duration, Instant},
};

enum Output {
    Namespace(Namespace),
    Assets(Vec<Asset>),
}
struct Job {
    owner: Context,
    command: Command,
    branch: Option<BranchId>,
    postgres: bool,
    namespace: Option<Namespace>,
    adapter: Option<Adapter>,
    worker: Option<JoinHandle<RemoteResult<Output>>>,
    result: Option<Value>,
    started: Instant,
}
#[derive(Default)]
pub struct Service {
    jobs: HashMap<OperationId, Job>,
}
impl Service {
    pub fn recover(store: &mut Store) -> Result<Self> {
        store.recover_catalog_metadata()?;
        Ok(Self::default())
    }
    pub fn active(&self) -> usize {
        self.jobs.values().filter(|j| j.worker.is_some()).count()
    }
    pub fn branch(store: &Store, binding: &Binding, command: &Command) -> Result<Option<BranchId>> {
        match command {
            Command::List { branch, .. } => Ok(Some(crate::api::resolve(
                store,
                binding,
                branch.as_deref(),
            )?)),
            Command::Describe { id }
            | Command::Resolve { id, .. }
            | Command::ValidateSource { id, .. } => {
                let owner = store.binding_context(binding)?;
                let a = store.catalog_asset(owner.deployment_id, *id)?;
                if a.kind == "postgres_table" {
                    Ok(Some(a.branch_id))
                } else {
                    Ok(None)
                }
            }
            _ => Ok(None),
        }
    }
    pub fn handle(
        &mut self,
        store: &mut Store,
        manager: &Manager,
        binding: &Binding,
        command: Command,
        target: Option<Value>,
    ) -> Result<Value> {
        binding.validate(store)?;
        if binding.worktree == store.root().join("console-home") {
            return Err(conflict("select a project before using catalog metadata"));
        }
        self.jobs
            .retain(|_, j| j.worker.is_some() || j.started.elapsed() < Duration::from_secs(300));
        let owner = store.binding_context(binding)?;
        match &command {
            Command::Capabilities {} => return Ok(capabilities()),
            Command::Health {} => {
                return Ok(
                    json!({"api_version":VERSION,"deployment_id":owner.deployment_id,"catalog":manager.status()}),
                );
            }
            Command::Poll { id } => {
                let j = self
                    .jobs
                    .get(id)
                    .filter(|j| j.owner.deployment_id == owner.deployment_id)
                    .ok_or_else(|| {
                        missing(
                            "catalog request in deployment (expired requests must be resubmitted)",
                        )
                    })?;
                return Ok(j
                    .result
                    .clone()
                    .unwrap_or_else(|| json!({"api_version":VERSION,"id":id,"state":"running"})));
            }
            Command::List { limit, after, .. } => {
                if !(1..=100).contains(limit) || after.as_ref().is_some_and(|s| s.len() > 2048) {
                    return Err(invalid(
                        "catalog pages require 1–100 results and a bounded cursor",
                    ));
                }
            }
            Command::Resolve {
                expected_version, ..
            }
            | Command::ValidateSource {
                expected_version, ..
            } => check_version(expected_version)?,
            _ => (),
        }

        if self.jobs.len() >= 32 {
            let oldest = self
                .jobs
                .iter()
                .filter(|(_, j)| j.worker.is_none())
                .min_by_key(|(_, j)| j.started)
                .map(|(id, _)| *id);
            if let Some(id) = oldest {
                self.jobs.remove(&id);
            }
        }
        if self.active() >= 2 || self.jobs.len() >= 32 {
            return Err(supabricks_core::error::OperationError::Unavailable(
                "catalog request capacity reached; retry after completed requests expire".into(),
            )
            .into());
        }
        let mut namespace = None;
        let mut adapter = None;
        let mut branch = None;
        let mut postgres = false;
        let worker = match &command {
            Command::Namespace {} | Command::EnsureNamespace {} => {
                let provider = manager.adapter(store)?;
                let existing =
                    store.catalog_namespace(owner.deployment_id, &provider.provider_id)?;
                if matches!(command, Command::Namespace {})
                    && existing.as_ref().is_none_or(|n| n.state != "ready")
                {
                    return Ok(
                        json!({"api_version":VERSION,"namespace":existing,"catalog":manager.status()}),
                    );
                }
                if let Some((id, _)) = self.jobs.iter().find(|(_, j)| {
                    j.worker.is_some()
                        && j.owner.deployment_id == owner.deployment_id
                        && j.namespace
                            .as_ref()
                            .is_some_and(|n| n.provider_id == provider.provider_id)
                }) {
                    return Ok(json!({"api_version":VERSION,"id":id,"state":"running"}));
                }
                let mut n = existing.unwrap_or_else(|| {
                    let name = store
                        .project(owner.runtime_project_id)
                        .map(|p| p.name)
                        .unwrap_or_else(|_| "project".into());
                    let slug: String = name
                        .chars()
                        .filter(char::is_ascii_alphanumeric)
                        .take(20)
                        .flat_map(char::to_lowercase)
                        .collect();
                    Namespace {
                        deployment_id: owner.deployment_id,
                        project_id: owner.runtime_project_id,
                        provider_id: provider.provider_id.clone(),
                        metastore_id: provider.probe.expected_metastore.clone().unwrap(),
                        catalog: format!(
                            "sb_{}_{}",
                            slug,
                            owner.deployment_id.to_string().replace('-', "")
                        ),
                        schema: "analytics".into(),
                        catalog_id: None,
                        schema_id: None,
                        state: "new".into(),
                    }
                });
                if matches!(n.state.as_str(), "indeterminate" | "stale" | "collision") {
                    return Err(conflict(
                        "catalog namespace requires explicit reconciliation; an existing name cannot establish ownership",
                    ));
                }
                if n.metastore_id != provider.probe.expected_metastore.clone().unwrap() {
                    return Err(conflict("namespace metastore identity changed"));
                }
                n.state = if n.catalog_id.is_none() {
                    "creating_catalog"
                } else if n.schema_id.is_none() {
                    "creating_schema"
                } else {
                    "ready"
                }
                .into();
                store.save_catalog_namespace(&n)?;
                namespace = Some(n.clone());
                adapter = Some(provider.clone());
                std::thread::spawn(move || provider.namespace(n).map(Output::Namespace))
            }
            Command::List { .. }
            | Command::Describe { .. }
            | Command::Resolve { .. }
            | Command::ValidateSource { .. } => {
                // A snapshot carries its recorded source observation; live PG is freshly queried.
                let selected = match &command {
                    Command::List { branch, .. } => {
                        crate::api::resolve(store, binding, branch.as_deref())?
                    }
                    Command::Describe { id }
                    | Command::Resolve { id, .. }
                    | Command::ValidateSource { id, .. } => {
                        store.catalog_asset(owner.deployment_id, *id)?.branch_id
                    }
                    _ => unreachable!(),
                };
                store.branch_in_project(owner.runtime_project_id, selected)?;
                let snapshot = match &command {
                    Command::List { .. } => {
                        match store.current_snapshot(owner.runtime_project_id, selected) {
                            Ok(s) => Some(s),
                            Err(crate::store::Error::Operation(
                                supabricks_core::error::OperationError::NotFound(_),
                            )) => None,
                            Err(e) => return Err(e),
                        }
                    }
                    Command::Describe { id }
                    | Command::Resolve { id, .. }
                    | Command::ValidateSource { id, .. } => {
                        let a = store.catalog_asset(owner.deployment_id, *id)?;
                        a.epoch_id
                            .map(|id| store.snapshot(owner.runtime_project_id, id))
                            .transpose()?
                    }
                    _ => None,
                };
                postgres = target.is_some();
                branch = Some(selected);
                let installation = store.installation_id()?;
                let context = owner.clone();
                let revision = store.branch(selected)?.revision;
                std::thread::spawn(move || {
                    let mut assets = if let Some(target) = target {
                        sources::postgres(&context, selected, revision, &installation, target)?
                    } else {
                        vec![]
                    };
                    if let Some(snapshot) = snapshot {
                        assets.extend(sources::snapshot(
                            &context,
                            selected,
                            &installation,
                            snapshot,
                        )?);
                    }
                    if serde_json::to_vec(&assets).map_or(true, |v| v.len() > MAX_BYTES - 8192) {
                        return Err(Fault::new(
                            Code::LimitExceeded,
                            "metadata response exceeds byte limit",
                        ));
                    }
                    Ok(Output::Assets(assets))
                })
            }
            _ => unreachable!(),
        };
        let id = OperationId::new();
        self.jobs.insert(
            id,
            Job {
                owner,
                command,
                branch,
                postgres,
                namespace,
                adapter,
                worker: Some(worker),
                result: None,
                started: Instant::now(),
            },
        );
        Ok(json!({"api_version":VERSION,"id":id,"state":"running"}))
    }
    pub fn tick(&mut self, store: &mut Store, manager: &Manager, stopping: bool) -> Result<bool> {
        for (id, j) in &mut self.jobs {
            if !j.worker.as_ref().is_some_and(|w| w.is_finished()) {
                continue;
            }
            let output = j.worker.take().unwrap().join().unwrap_or_else(|_| {
                Err(Fault::new(Code::Unavailable, "catalog worker terminated"))
            });
            let completion: Result<Option<RemoteResult<Value>>> = (|| {
                let result = match output {
                    Ok(Output::Namespace(mut n)) => {
                        if manager.adapter(store).map_or(true, |a| {
                            a.provider_id != n.provider_id
                                || a.probe.expected_metastore.as_ref() != Some(&n.metastore_id)
                        }) {
                            n.state = "indeterminate".into();
                            store.save_catalog_namespace(&n)?;
                            Err(Fault::new(
                                Code::IdentityChanged,
                                "catalog provider changed during request",
                            ))
                        } else {
                            store.save_catalog_namespace(&n)?;
                            if n.state == "catalog_ready" && !stopping {
                                n.state = "creating_schema".into();
                                store.save_catalog_namespace(&n)?;
                                j.namespace = Some(n.clone());
                                let a = j.adapter.clone().unwrap();
                                j.worker = Some(std::thread::spawn(move || {
                                    a.namespace(n).map(Output::Namespace)
                                }));
                                return Ok(None);
                            }
                            Ok(json!({"namespace":n}))
                        }
                    }
                    Ok(Output::Assets(assets)) => {
                        let assets = store.observe_catalog_assets(
                            &j.owner,
                            j.branch.unwrap(),
                            assets,
                            j.postgres,
                        )?;
                        match &j.command {
                            Command::List { after, limit, .. } => page(
                                &j.owner,
                                j.branch.unwrap(),
                                assets,
                                after.as_deref(),
                                *limit,
                            ),
                            Command::Describe { id }
                            | Command::Resolve { id, .. }
                            | Command::ValidateSource { id, .. } => {
                                let found = assets.iter().find(|a| a.id == *id);
                                if let Some(a) = found {
                                    match &j.command {
                                    Command::Resolve {
                                        expected_version, ..
                                    }
                                    | Command::ValidateSource {
                                        expected_version, ..
                                    } if expected_version != &a.version => Err(Fault::new(
                                        if matches!(j.command, Command::ValidateSource { .. }) {
                                            Code::SchemaDrift
                                        } else {
                                            Code::StaleVersion
                                        },
                                        "asset alias, incarnation or schema changed; inspect and explicitly rebind",
                                    )),
                                    Command::ValidateSource { .. } => sources::validate_source(
                                        &assets,
                                    )
                                    .map(
                                        |_| json!({"asset":a,"valid":true,"storage_access":false}),
                                    ),
                                    _ => Ok(json!({"asset":a,"storage_access":false})),
                                }
                                } else {
                                    store.stale_catalog_asset(j.owner.deployment_id, *id)?;
                                    Err(Fault::new(
                                        Code::IdentityChanged,
                                        "asset was deleted or recreated; rediscover its current identity",
                                    ))
                                }
                            }
                            _ => unreachable!(),
                        }
                    }
                    Err(error) => {
                        if let Some(n) = &mut j.namespace {
                            n.state = match error.code {
                                Code::NameCollision => "collision",
                                Code::NotFound | Code::IdentityChanged => "stale",
                                _ if n.state == "ready" => "ready",
                                _ => "indeterminate",
                            }
                            .into();
                            store.save_catalog_namespace(n)?;
                        }
                        Err(error)
                    }
                };
                Ok(Some(result))
            })();
            let result = match completion {
                Ok(Some(result)) => result,
                Ok(None) => continue,
                Err(_) => Err(Fault::new(
                    Code::Unavailable,
                    "catalog control-state update failed; inspect local state before retrying",
                )),
            };
            j.result = Some(match result {
                Ok(value) => {
                    json!({"api_version":VERSION,"id":id,"state":"complete","result":value,"catalog":manager.status()})
                }
                Err(error) => json!({"api_version":VERSION,"id":id,"state":"failed","error":error}),
            });
        }
        Ok(!stopping || self.active() == 0)
    }
}
pub(super) fn page(
    owner: &Context,
    branch: BranchId,
    mut assets: Vec<Asset>,
    after: Option<&str>,
    limit: usize,
) -> RemoteResult<Value> {
    assets.sort_by_key(|a| a.id.to_string());
    let revision = fingerprint(&json!([
        owner.deployment_id,
        branch,
        assets
            .iter()
            .map(|a| (&a.id, &a.version))
            .collect::<Vec<_>>()
    ]));
    let start = if let Some(cursor) = after {
        let parts: Value = hex::decode(cursor)
            .ok()
            .and_then(|v| serde_json::from_slice(&v).ok())
            .ok_or_else(|| Fault::new(Code::StaleVersion, "invalid metadata cursor"))?;
        if parts["revision"] != revision {
            return Err(Fault::new(
                Code::StaleVersion,
                "metadata changed; restart pagination",
            ));
        }
        let position = parts["offset"]
            .as_u64()
            .filter(|v| *v <= assets.len() as u64)
            .ok_or_else(|| Fault::new(Code::StaleVersion, "invalid metadata cursor offset"))?;
        position as usize
    } else {
        0
    };
    let end = (start + limit).min(assets.len());
    let next = if end < assets.len() {
        Some(hex::encode(
            json!({"revision":revision,"offset":end}).to_string(),
        ))
    } else {
        None
    };
    Ok(json!({"assets":&assets[start..end],"next":next,"revision":revision}))
}
