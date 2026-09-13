//! Browser-owned adapters over the existing A02/A03 publication and lease machinery.
use super::workspace::Target;
use crate::{
    api::Binding,
    sessions::Sessions,
    store::{
        AnalyticalSession, Result, Store,
        error::{conflict, missing},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    time::{Duration, Instant},
};
use supabricks_core::resource::{OperationId, ProjectId};

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Snapshot {
        target: Target,
    },
    Refresh {
        target: Target,
        key: String,
    },
    RefreshStatus {
        id: OperationId,
    },
    CancelRefresh {
        id: OperationId,
    },
    Open {
        target: Target,
        key: String,
    },
    List,
    Status {
        id: OperationId,
    },
    Close {
        id: OperationId,
    },
    Cancel {
        id: OperationId,
    },
    Sql {
        id: OperationId,
        sql: String,
        max_rows: usize,
        timeout_ms: u64,
    },
}
struct Owned {
    scope: String,
    project: ProjectId,
    seen: Instant,
}
#[derive(Default)]
pub(crate) struct Workspace {
    sessions: HashMap<OperationId, Owned>,
}
fn key(scope: &str, key: &str) -> Result<String> {
    crate::notebooks::contract::key(key)?;
    Ok(format!(
        "console-analytics:{}",
        hex::encode(Sha256::digest(json!([scope, key]).to_string().as_bytes()))
    ))
}
fn session(s: AnalyticalSession) -> Value {
    // Never return the worker endpoint, filesystem paths, or raw worker diagnostics.
    let error = s.error.as_deref().map(|reason| match reason {
        "closed" => "Session closed.",
        "cancelled" => "Session cancelled.",
        "expired" => "Session expired. Open a new session explicitly.",
        "daemon_restarted" => "Runtime restarted. Open a new session explicitly.",
        "console_disconnected" => "Browser heartbeat expired. Open a new session explicitly.",
        _ => "Session failed; inspect analytics session status with the CLI for diagnostics.",
    });
    let mut query = s.query;
    if let Some(q) = query.as_mut() {
        if q.get("error").is_some_and(|e| !e.is_null()) {
            q["error"] = json!(
                "Analytics SQL failed or was cancelled. Check read-only SQL, supported types, and result limits; inspect CLI diagnostics."
            );
        }
    }
    json!({"id":s.id,"branch_id":s.branch_id,"epoch_id":s.epoch_id,"state":s.state,"expires_at_ms":s.expires_at_ms,"metadata":s.metadata,"query":query,"error":error})
}
fn publication(p: crate::store::Publication) -> Value {
    let d = p.descriptor.unwrap_or(Value::Null);
    let tables = d["manifest"]["tables"].as_array().map(|tables| {
        tables
            .iter()
            .map(|table| {
                let columns = table["columns"].as_array().map(|columns| {
                    columns
                        .iter()
                        .map(|column| {
                            json!({
                                "name": column["name"], "type": column["arrow_type"]
                            })
                        })
                        .collect::<Vec<_>>()
                });
                json!({"schema": table["schema"], "name": table["name"], "columns": columns})
            })
            .collect::<Vec<_>>()
    });
    json!({"epoch_id":p.epoch_id,"ordinal":p.ordinal,"branch_id":p.branch_id,"source_revision":p.source_revision,"observed_at_ms":d["manifest"]["observed_at_ms"],"published_at_ms":p.published_at_ms,"source":d["manifest"]["source"],"database":d["manifest"]["database"],"tables":tables})
}
fn refresh(v: Value) -> Value {
    let failed = v["state"] == "failed" || v["state"] == "cancelled";
    json!({"id":v["id"],"state":v["state"],"branch_id":v["export"]["source_id"],"error":failed.then_some("Refresh failed or was cancelled. The previous publication is preserved. JSONB and other unsupported source types must be resolved before refreshing; inspect analytics status with the CLI."),"epoch_id":v["publication"]["epoch_id"]})
}
impl Workspace {
    pub fn tick(&mut self, store: &mut Store, stopping: bool) -> Result<()> {
        for (id, owner) in &self.sessions {
            if stopping || owner.seen.elapsed() > Duration::from_secs(120) {
                store.close_analytical_session(owner.project, *id, "console_disconnected")?;
            }
        }
        self.sessions
            .retain(|_, o| o.seen.elapsed() < Duration::from_secs(600));
        Ok(())
    }
    pub fn handle(
        &mut self,
        store: &mut Store,
        cell: Option<&crate::engine::Cell>,
        binding: &Binding,
        scope: &str,
        command: Command,
    ) -> Result<Value> {
        let project = binding.project_id;
        match command {
            Command::Snapshot { target } => {
                store.branch_in_project(project, target.branch)?;
                match store.current_snapshot(project, target.branch) {
                    Ok(s) => Ok(publication(s.publication)),
                    Err(crate::store::Error::Operation(
                        supabricks_core::error::OperationError::NotFound(_),
                    )) => Ok(Value::Null),
                    Err(e) => Err(e),
                }
            }
            Command::Refresh { target, key: k } => {
                target.validate(store, binding)?;
                Ok(refresh(Sessions::refresh(
                    store,
                    cell,
                    binding,
                    target.branch.to_string(),
                    key(scope, &k)?,
                    Default::default(),
                )?))
            }
            Command::RefreshStatus { id } => Ok(refresh(store.refresh_status(project, id)?)),
            Command::CancelRefresh { id } => {
                Ok(refresh(Sessions::cancel_refresh(store, project, id)?))
            }
            Command::Open { target, key: k } => {
                target.validate(store, binding)?;
                if self.sessions.len() >= 128 {
                    return Err(conflict(
                        "Console session history is full; allow expired handles to clear",
                    ));
                }
                // Explicit publication first: no orphaned implicit refresh if the browser disappears.
                let epoch = store
                    .current_snapshot(project, target.branch)?
                    .publication
                    .epoch_id;
                let v = Sessions::open(
                    store,
                    cell,
                    binding,
                    Some(target.branch.to_string()),
                    Some(epoch),
                    key(scope, &k)?,
                    900_000,
                )?;
                let s: AnalyticalSession = serde_json::from_value(v)?;
                self.sessions.insert(
                    s.id,
                    Owned {
                        scope: scope.into(),
                        project,
                        seen: Instant::now(),
                    },
                );
                Ok(session(s))
            }
            Command::List => {
                let mut values = Vec::new();
                for (id, o) in self.sessions.iter_mut().filter(|(_, o)| o.scope == scope) {
                    let mut s = store.analytical_session(project, *id)?;
                    if matches!(s.state.as_str(), "waiting" | "starting" | "ready") {
                        o.seen = Instant::now();
                    }
                    s.query = None;
                    values.push(session(s));
                }
                values.sort_by_key(|v| v["id"].as_str().unwrap_or_default().to_owned());
                Ok(json!(values))
            }
            other => {
                let id = match &other {
                    Command::Status { id }
                    | Command::Close { id }
                    | Command::Cancel { id }
                    | Command::Sql { id, .. } => *id,
                    _ => unreachable!(),
                };
                let o=self.sessions.get_mut(&id).filter(|o|o.scope==scope).ok_or_else(||missing("Analytical session belongs to another browser or an earlier daemon generation; reopen explicitly"))?;
                if matches!(
                    store.analytical_session(project, id)?.state.as_str(),
                    "waiting" | "starting" | "ready"
                ) {
                    o.seen = Instant::now();
                }
                match other {
                    Command::Status { .. } => Ok(session(store.analytical_session(project, id)?)),
                    Command::Close { .. } => Ok(session(
                        store.close_analytical_session(project, id, "closed")?,
                    )),
                    Command::Cancel { .. } => Ok(session(store.close_analytical_session(
                        project,
                        id,
                        "cancelled",
                    )?)),
                    Command::Sql {
                        sql,
                        max_rows,
                        timeout_ms,
                        ..
                    } => {
                        Sessions::query(store, project, id, sql, max_rows, 262144, timeout_ms)?;
                        Ok(session(store.analytical_session(project, id)?))
                    }
                    _ => unreachable!(),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        operations::{Mutation, Ports},
        project::ProjectConfig,
    };
    use supabricks_core::resource::BranchId;
    fn waiting(store: &mut Store) -> (Binding, AnalyticalSession) {
        let project = ProjectConfig {
            format_version: 1,
            id: ProjectId::new(),
            name: "analytical-console".into(),
        };
        store.register_project(&project).unwrap();
        let ports = |n| Ports {
            sql: n,
            external_http: n + 1,
            internal_http: n + 2,
        };
        let branch = store
            .submit(
                project.id,
                "root",
                Mutation::CreateBranch {
                    name: "main".into(),
                    parent_id: None,
                    ports: ports(5400),
                },
            )
            .unwrap();
        while let Some(ticket) = store.ticket(branch.id).unwrap() {
            store.checkpoint(&ticket, json!({})).unwrap();
        }
        let export = store
            .submit(
                project.id,
                "refresh",
                Mutation::Export {
                    parent_id: branch.branch_id,
                    ports: ports(5403),
                    limits: Default::default(),
                },
            )
            .unwrap();
        let s = store
            .admit_analytical_session(
                project.id,
                branch.branch_id,
                "reader",
                json!({}),
                None,
                Some(export.id),
                900_000,
            )
            .unwrap();
        (
            Binding {
                project_id: project.id,
                worktree: store.root().join("app"),
            },
            s,
        )
    }
    #[test]
    fn abandoned_browser_closes_without_foreign_heartbeat_or_terminal_retention() {
        let temp = tempfile::tempdir().unwrap();
        let mut store = Store::open(&temp.path().join("data")).unwrap();
        let (binding, s) = waiting(&mut store);
        let mut ui = Workspace::default();
        ui.sessions.insert(
            s.id,
            Owned {
                scope: "owner".into(),
                project: binding.project_id,
                seen: Instant::now() - Duration::from_secs(121),
            },
        );
        assert_eq!(
            ui.handle(&mut store, None, &binding, "stranger", Command::List)
                .unwrap(),
            json!([])
        );
        assert!(
            ui.handle(
                &mut store,
                None,
                &binding,
                "stranger",
                Command::Cancel { id: s.id }
            )
            .is_err()
        );
        ui.tick(&mut store, false).unwrap();
        assert_eq!(
            store
                .analytical_session(binding.project_id, s.id)
                .unwrap()
                .state,
            "closing"
        );
        let seen = ui.sessions[&s.id].seen;
        ui.handle(&mut store, None, &binding, "owner", Command::List)
            .unwrap();
        assert_eq!(
            ui.sessions[&s.id].seen, seen,
            "listing closed history must not renew retention"
        );
        ui.sessions.get_mut(&s.id).unwrap().seen = Instant::now() - Duration::from_secs(601);
        ui.tick(&mut store, false).unwrap();
        assert!(ui.sessions.is_empty());
    }
    #[test]
    fn session_projection_preserves_result_identity_without_private_worker_diagnostics() {
        let s = AnalyticalSession {
            id: OperationId::new(),
            project_id: ProjectId::new(),
            branch_id: BranchId::new(),
            epoch_id: None,
            refresh_id: None,
            state: "failed".into(),
            created_at_ms: 0,
            expires_at_ms: 1,
            endpoint: Some("sc://private-endpoint".into()),
            metadata: None,
            error: Some("/private/worker/path".into()),
            query: Some(json!({"id":"query","state":"failed","error":"sc://private-endpoint"})),
        };
        let value = session(s);
        let wire = value.to_string();
        assert!(!wire.contains("private"));
        assert_eq!(value["query"]["id"], "query");
        assert!(!value["query"]["error"].as_str().unwrap().is_empty());
    }
}
