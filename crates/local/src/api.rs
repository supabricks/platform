//! Local application API v1. Independent of the operator's HTTP/MCP contract.
use crate::{
    operations::{BranchPoint, Mutation, Ports},
    project::ProjectConfig,
    store::{
        Result, Store,
        error::{conflict, invalid, missing},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::HashSet, net::TcpListener, path::PathBuf};
use supabricks_core::resource::{BranchId, DesiredState, EpochId, LeaseId, OperationId, ProjectId};

pub const VERSION: u32 = 1;
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub project_id: ProjectId,
    pub worktree: PathBuf,
}
impl Binding {
    pub fn validate(&self, store: &mut Store) -> Result<()> {
        if !self.worktree.is_absolute() {
            return Err(invalid("worktree must be absolute"));
        }
        let config = ProjectConfig::read(&self.worktree)?;
        if config.id != self.project_id {
            return Err(invalid(
                "project identity changed; reopen the CLI or MCP session",
            ));
        }
        store.register_project(&config)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Action {
    Environment {
        command: crate::environments::Command,
    },
    IngestInspect {
        path: std::path::PathBuf,
        #[serde(default = "ingest_delimiter")]
        delimiter: String,
        #[serde(default = "ingest_header")]
        header: bool,
        #[serde(default)]
        null_strings: Vec<String>,
    },
    IngestSource {
        id: crate::ingest::SourceId,
    },
    IngestLoad {
        load: crate::ingest::Load,
        key: String,
    },
    IngestStatus {
        id: crate::ingest::JobId,
    },
    IngestList {
        branch: Option<String>,
        #[serde(default = "ingest_limit")]
        limit: usize,
    },
    IngestFind {
        branch: String,
        key: String,
    },
    IngestCancel {
        id: crate::ingest::JobId,
    },
    IngestRetry {
        id: crate::ingest::JobId,
    },
    IngestDispose {
        id: crate::ingest::SourceId,
    },
    AnalyticsRefresh {
        branch: String,
        key: String,
        #[serde(default)]
        limits: crate::store::ExportLimits,
    },
    AnalyticsStatus {
        id: OperationId,
    },
    AnalyticsCancelRefresh {
        id: OperationId,
    },
    AnalyticsOpen {
        branch: Option<String>,
        epoch: Option<EpochId>,
        key: String,
        #[serde(default = "session_ttl")]
        ttl_ms: u64,
    },
    AnalyticsSession {
        id: OperationId,
    },
    AnalyticsClose {
        id: OperationId,
    },
    AnalyticsSql {
        id: OperationId,
        sql: String,
        #[serde(default = "rows")]
        max_rows: usize,
        #[serde(default = "analytical_bytes")]
        max_bytes: usize,
        #[serde(default = "timeout")]
        timeout_ms: u64,
    },
    AnalyticsQuery {
        id: OperationId,
        query: OperationId,
    },
    AnalyticsCancel {
        id: OperationId,
    },
    PublishExport {
        id: OperationId,
    },
    GetPublication {
        id: OperationId,
    },
    DiscardExport {
        id: OperationId,
    },
    CurrentSnapshot {
        branch: String,
    },
    ListSnapshots {
        branch: String,
        #[serde(default)]
        before: Option<i64>,
        #[serde(default = "snapshot_page_size")]
        limit: usize,
    },
    GetSnapshot {
        id: EpochId,
    },
    PinSnapshot {
        id: EpochId,
        ttl_ms: u64,
    },
    RenewSnapshotLease {
        id: LeaseId,
        ttl_ms: u64,
    },
    ReleaseSnapshotLease {
        id: LeaseId,
    },
    CollectSnapshots {
        branch: String,
        keep: usize,
    },
    ConfigureAnalytics {
        python: PathBuf,
        worker: PathBuf,
    },
    Export {
        branch: String,
        key: String,
        #[serde(default)]
        limits: crate::store::ExportLimits,
    },
    GetExport {
        id: OperationId,
    },
    CancelExport {
        id: OperationId,
    },
    Capabilities,
    ListBranches {
        #[serde(default)]
        include_deleted: bool,
    },
    GetBranch {
        branch: String,
    },
    Selection,
    SelectBranch {
        branch: String,
    },
    CreateDatabase {
        name: String,
        key: String,
    },
    CreateBranch {
        name: String,
        parent: String,
        key: String,
        #[serde(default)]
        point: BranchPoint,
    },
    SetState {
        branch: String,
        expected_revision: i64,
        desired: DesiredState,
        key: String,
    },
    DeleteBranch {
        branch: String,
        expected_revision: i64,
        key: String,
        #[serde(default)]
        force: bool,
    },
    RenameBranch {
        branch: String,
        name: String,
    },
    SetDefault {
        branch: String,
        key: String,
    },
    SetTtl {
        branch: String,
        expected_revision: i64,
        #[serde(deserialize_with = "required_nullable")]
        expires_at_ms: Option<i64>,
        key: String,
    },
    GetOperation {
        id: OperationId,
    },
    Connect {
        branch: Option<String>,
    },
    Catalog {
        branch: Option<String>,
    },
    Sql {
        branch: Option<String>,
        sql: String,
        #[serde(default = "yes")]
        read_only: bool,
        #[serde(default = "rows")]
        max_rows: usize,
        #[serde(default = "timeout")]
        timeout_ms: u64,
    },
}
fn session_ttl() -> u64 {
    900_000
}
fn analytical_bytes() -> usize {
    262144
}
fn required_nullable<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<i64>, D::Error> {
    Option::<i64>::deserialize(d)
}
fn snapshot_page_size() -> usize {
    100
}
fn yes() -> bool {
    true
}
fn rows() -> usize {
    200
}
fn timeout() -> u64 {
    10_000
}

pub fn capabilities(binding: &Binding) -> Value {
    json!({"api":"supabricks.local", "api_version":VERSION,"project_id":binding.project_id,"worktree":binding.worktree,
        "postgres_major":17,"features":{"branching":true,"stable_connections":true,"wake_on_connect":true,"automatic_idle_suspend":false,"analytics":true,"unpublished_exports":true,"atomic_snapshots":true,"ingestion":true,"notebook_environments":true,"notebook_packages":true},
        "limits":{"request_bytes":65536,"sql_bytes":32768,"sql_rows":1000,"sql_result_bytes":262144,"sql_frame_bytes":1048576,"sql_timeout_ms":30000,"sql_workers":4,"sql_total_deadline_ms":45000,"analytical_sessions":2,"analytical_session_ttl_ms":3600000,"analytical_sql_rows":1000,"analytical_sql_result_bytes":262144,"analytical_sql_timeout_ms":30000,"active_branches":32,"connections":256,"connections_per_branch":64},
        "ingestion":{"formats":["csv","tsv"],"new_tables_only":true,"approved_mapping_required":true,"source_bytes":104857600,"decoded_bytes":536870912,"sampled_rss_bytes":536870912,"preview_rows":100,"preview_bytes":262144,"active_imports":1,"deadline_ms":600000},
        "sql":{"read_only_default":true,"statements_per_call":1,"values":"PostgreSQL text or null","writes":"explicit read_only=false; no automatic retry"}})
}
pub(crate) fn resolve(store: &Store, binding: &Binding, target: Option<&str>) -> Result<BranchId> {
    let id = match target {
        None => store.selected_branch(&binding.worktree, binding.project_id)?,
        Some(s) => match s.parse::<BranchId>() {
            Ok(id) => id,
            Err(_) => {
                store
                    .list_branches(binding.project_id, false)?
                    .into_iter()
                    .find(|b| b.branch.name == s)
                    .ok_or_else(|| missing(format!("branch {s} in project")))?
                    .branch
                    .id
            }
        },
    };
    store.branch_in_project(binding.project_id, id)?;
    Ok(id)
}
/// All metadata transitions run on the daemon's single writer.
pub(crate) fn handle(
    store: &mut Store,
    cell: Option<&crate::engine::Cell>,
    binding: &Binding,
    action: Action,
) -> Result<Value> {
    let project = binding.project_id;
    let mut held_ports = Vec::new();
    let (key, mutation) = match action {
        Action::IngestInspect { .. } => {
            return Err(invalid("inspection requires daemon worker service"));
        }
        Action::IngestSource { id } => {
            return crate::ingest::service::source_status(store, project, id);
        }
        Action::IngestLoad { load, key } => {
            crate::ingest::service::worker(store)?;
            if load.project_id != project {
                return Err(conflict("ingestion project differs from session binding"));
            }
            load.validate()?;
            if load.mapping.format != crate::ingest::Format::Csv
                || load
                    .mapping
                    .columns
                    .iter()
                    .any(|c| matches!(c.data_type, crate::ingest::DataType::Jsonb))
            {
                return Err(invalid("I01 supports CSV/TSV and scalar columns"));
            }
            if store
                .ingest_for_key(project, load.branch_id, &key)?
                .is_none()
            {
                store.connection(project, load.branch_id)?;
            }
            let j = store.create_ingest(&key, load)?;
            return crate::ingest::service::status(store, project, j.id);
        }
        Action::IngestStatus { id } => return crate::ingest::service::status(store, project, id),
        Action::IngestFind { branch, key } => {
            let branch = resolve(store, binding, Some(&branch))?;
            return Ok(json!(store.ingest_for_key(project, branch, &key)?));
        }
        Action::IngestList { branch, limit } => {
            let branch = branch
                .map(|b| resolve(store, binding, Some(&b)))
                .transpose()?;
            let mut jobs = Vec::new();
            let mut bytes = 0;
            for job in store.ingest_list(project, branch, limit)? {
                bytes += serde_json::to_vec(&job)?.len();
                if bytes > 1024 * 1024 {
                    break;
                }
                jobs.push(job);
            }
            return Ok(json!(jobs));
        }
        Action::IngestCancel { id } => {
            store.cancel_ingest(project, id)?;
            return crate::ingest::service::status(store, project, id);
        }
        Action::IngestRetry { id } => {
            store.retry_ingest(project, id)?;
            return crate::ingest::service::status(store, project, id);
        }
        Action::IngestDispose { id } => {
            store.dispose_source(project, id, false)?;
            return Ok(json!({"disposed":true,"source_id":id}));
        }
        Action::AnalyticsRefresh {
            branch,
            key,
            limits,
        } => return crate::sessions::Sessions::refresh(store, cell, binding, branch, key, limits),
        Action::AnalyticsStatus { id } => return store.refresh_status(project, id),
        Action::AnalyticsCancelRefresh { id } => {
            return crate::sessions::Sessions::cancel_refresh(store, project, id);
        }
        Action::AnalyticsOpen {
            branch,
            epoch,
            key,
            ttl_ms,
        } => {
            return crate::sessions::Sessions::open(
                store, cell, binding, branch, epoch, key, ttl_ms,
            );
        }
        Action::AnalyticsSession { id } => {
            let session = store.analytical_session(project, id)?;
            let observed = session
                .metadata
                .as_ref()
                .and_then(|m| m["observed_at_ms"].as_i64());
            let mut value = json!(session);
            value["snapshot_age_ms"] =
                json!(observed.map(|t| (chrono::Utc::now().timestamp_millis() - t).max(0)));
            return Ok(value);
        }
        Action::AnalyticsClose { id } => {
            return Ok(json!(
                store.close_analytical_session(project, id, "closed")?
            ));
        }
        Action::AnalyticsCancel { id } => {
            return Ok(json!(store.close_analytical_session(
                project,
                id,
                "cancelled"
            )?));
        }
        Action::AnalyticsSql {
            id,
            sql,
            max_rows,
            max_bytes,
            timeout_ms,
        } => {
            return crate::sessions::Sessions::query(
                store, project, id, sql, max_rows, max_bytes, timeout_ms,
            );
        }
        Action::AnalyticsQuery { id, query } => {
            return crate::sessions::Sessions::query_status(store, project, id, query);
        }
        Action::Capabilities => return Ok(capabilities(binding)),
        Action::PublishExport { id } => return Ok(json!(store.publish_export(project, id)?)),
        Action::GetPublication { id } => {
            return Ok(json!(store.publication_in_project(project, id)?));
        }
        Action::DiscardExport { id } => return store.discard_export(project, id),
        Action::CurrentSnapshot { branch } => {
            return Ok(json!(store.current_snapshot(
                project,
                resolve(store, binding, Some(&branch))?
            )?));
        }
        Action::ListSnapshots {
            branch,
            before,
            limit,
        } => {
            let snapshots = store.snapshot_history(
                project,
                resolve(store, binding, Some(&branch))?,
                before,
                limit,
            )?;
            let next_before = if snapshots.len() == limit {
                snapshots.last().map(|s| s.publication.ordinal)
            } else {
                None
            };
            return Ok(json!({"snapshots":snapshots,"next_before":next_before}));
        }
        Action::GetSnapshot { id } => return Ok(json!(store.snapshot(project, id)?)),
        Action::PinSnapshot { id, ttl_ms } => {
            return Ok(json!(store.pin_snapshot(project, id, ttl_ms)?));
        }
        Action::RenewSnapshotLease { id, ttl_ms } => {
            return Ok(json!(store.renew_snapshot_lease(project, id, ttl_ms)?));
        }
        Action::ReleaseSnapshotLease { id } => {
            store.release_snapshot_lease(project, id)?;
            return Ok(json!({"released":true}));
        }
        Action::CollectSnapshots { branch, keep } => {
            return store.collect_snapshots(project, resolve(store, binding, Some(&branch))?, keep);
        }
        Action::ConfigureAnalytics { python, worker } => {
            return cell
                .ok_or_else(|| invalid("engine is disabled"))?
                .configure_exporter(store, python, worker);
        }
        Action::GetExport { id } => return Ok(json!(store.export_in_project(project, id)?)),
        Action::CancelExport { id } => return Ok(json!(store.cancel_export(project, id)?)),
        Action::Export {
            branch,
            key,
            limits,
        } => {
            if cell.is_none() {
                return Err(invalid("engine is disabled"));
            }
            let parent_id = resolve(store, binding, Some(&branch))?;
            let ports = allocate(store, cell, project, &key, &mut held_ports)?;
            (
                key,
                Mutation::Export {
                    parent_id,
                    ports,
                    limits,
                },
            )
        }
        Action::ListBranches { include_deleted } => {
            return Ok(json!({"branches":store.list_branches(project,include_deleted)?}));
        }
        Action::GetBranch { branch } => {
            return Ok(json!(store.branch(resolve(
                store,
                binding,
                Some(&branch)
            )?)?));
        }
        Action::Selection => return Ok(json!({"branch_id":resolve(store,binding,None)?})),
        Action::SelectBranch { branch } => {
            let id = resolve(store, binding, Some(&branch))?;
            store.accepting_work(id)?;
            store.select_worktree(&binding.worktree, project, id)?;
            return Ok(json!({"branch_id":id,"worktree":binding.worktree}));
        }
        Action::GetOperation { id } => {
            let op = store.operation(id)?;
            if op.project_id != project {
                return Err(missing("operation in project"));
            }
            return Ok(json!(op));
        }
        Action::RenameBranch { branch, name } => {
            let id = resolve(store, binding, Some(&branch))?;
            store.rename_branch(project, id, &name)?;
            return Ok(json!(store.branch(id)?));
        }
        Action::CreateDatabase { name, key } => {
            let ports = allocate(store, cell, project, &key, &mut held_ports)?;
            (key, Mutation::CreateDatabase { name, ports })
        }
        Action::CreateBranch {
            name,
            parent,
            key,
            point,
        } => {
            let parent_id = resolve(store, binding, Some(&parent))?;
            let ports = allocate(store, cell, project, &key, &mut held_ports)?;
            (
                key,
                Mutation::BranchFrom {
                    name,
                    parent_id,
                    ports,
                    point,
                    timeout_ms: 90_000,
                },
            )
        }
        Action::SetState {
            branch,
            expected_revision,
            desired,
            key,
        } => {
            if desired == DesiredState::Deleted {
                return Err(invalid("use delete_branch to remove a named resource"));
            }
            (
                key,
                Mutation::SetState {
                    branch_id: resolve(store, binding, Some(&branch))?,
                    expected_revision,
                    desired,
                },
            )
        }
        Action::DeleteBranch {
            branch,
            expected_revision,
            key,
            force,
        } => {
            let branch_id = resolve(store, binding, Some(&branch))?;
            (
                key,
                if force {
                    Mutation::ForceDelete {
                        branch_id,
                        expected_revision,
                    }
                } else {
                    Mutation::SetState {
                        branch_id,
                        expected_revision,
                        desired: DesiredState::Deleted,
                    }
                },
            )
        }
        Action::SetDefault { branch, key } => (
            key,
            Mutation::SetDefault {
                branch_id: resolve(store, binding, Some(&branch))?,
            },
        ),
        Action::SetTtl {
            branch,
            expected_revision,
            expires_at_ms,
            key,
        } => (
            key,
            Mutation::SetTtl {
                branch_id: resolve(store, binding, Some(&branch))?,
                expected_revision,
                expires_at_ms,
            },
        ),
        Action::Environment { .. }
        | Action::Connect { .. }
        | Action::Catalog { .. }
        | Action::Sql { .. } => {
            return Err(invalid("connection action requires the native gateway"));
        }
    };
    if let Some(cell) = cell {
        cell.validate_mutation(&mutation)?;
    }
    let export = matches!(mutation, Mutation::Export { .. });
    let op = store.submit(project, &key, mutation)?;
    // OS sockets stay bound through the durable port reservation. Native startup
    // rechecks ownership; another program taking a port afterwards fails closed.
    drop(held_ports);
    let mut result = if export {
        json!(store.export(op.id)?)
    } else {
        json!(op)
    };
    result["key"] = json!(key);
    Ok(result)
}
fn allocate(
    store: &Store,
    cell: Option<&crate::engine::Cell>,
    project: ProjectId,
    key: &str,
    held: &mut Vec<TcpListener>,
) -> Result<Ports> {
    if let Some(request) = store.request_for_key(project, key)? {
        match request {
            Mutation::CreateDatabase { ports, .. }
            | Mutation::CreateBranch { ports, .. }
            | Mutation::BranchFrom { ports, .. }
            | Mutation::Export { ports, .. } => return Ok(ports),
            _ => {
                return Err(crate::store::error::conflict(
                    "idempotency key already used by another operation",
                ));
            }
        }
    }
    let branches = store.branches()?;
    if branches.iter().filter(|b| b.ports.is_some()).count() >= 32 {
        return Err(crate::store::error::conflict(
            "local application limit of 32 active branches reached; remove an unused branch",
        ));
    }
    let reserved: HashSet<_> = branches
        .into_iter()
        .filter_map(|b| b.ports)
        .flat_map(|p| [p.sql, p.external_http, p.internal_http])
        .chain(store.connection_ports()?.into_iter().map(|(_, p)| p))
        .collect();
    let mut ports = Vec::new();
    for _ in 0..128 {
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        let port = listener.local_addr()?.port();
        if reserved.contains(&port) || cell.is_some_and(|c| c.reserved_port(port)) {
            continue;
        }
        ports.push(port);
        held.push(listener);
        if ports.len() == 3 {
            return Ok(Ports {
                sql: ports[0],
                external_http: ports[1],
                internal_http: ports[2],
            });
        }
    }
    Err(crate::store::error::conflict(
        "could not allocate three compute ports",
    ))
}

fn ingest_delimiter() -> String {
    ",".into()
}
fn ingest_header() -> bool {
    true
}
fn ingest_limit() -> usize {
    20
}
