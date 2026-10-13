//! Local-operator live PostgreSQL activity. No durable query text or automatic wake.
mod sql;
use crate::{
    api::Binding,
    console::workspace::Target,
    engine::Cell,
    store::{
        Result, Store,
        error::{conflict, invalid, missing},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
use supabricks_core::resource::{OperationId, ProjectId};
const RETENTION: Duration = Duration::from_secs(30);
const MAX_JOBS: usize = 16;
const WORKERS: usize = 2;
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    List {
        target: Target,
        #[serde(default)]
        include_query_text: bool,
    },
    Cancel {
        target: Target,
        snapshot: OperationId,
        pid: i32,
    },
    Terminate {
        target: Target,
        snapshot: OperationId,
        pid: i32,
    },
    Poll {
        id: OperationId,
    },
}
struct Job {
    project: ProjectId,
    target: Target,
    generation: i64,
    started: Instant,
    thread: Option<thread::JoinHandle<()>>,
    receiver: mpsc::Receiver<Result<Value>>,
    value: Value,
    // A snapshot/session can authorize one signal. Retries return its receipt.
    signals: HashMap<i32, (bool, OperationId)>,
}
#[derive(Default)]
pub struct Service {
    jobs: HashMap<OperationId, Job>,
}
impl Service {
    pub fn tick(&mut self) {
        for job in self.jobs.values_mut() {
            let Some(t) = &job.thread else { continue };
            if !t.is_finished() {
                continue;
            }
            let _ = job.thread.take().unwrap().join();
            match job.receiver.try_recv().unwrap_or_else(|_| {
                Err(conflict(
                    "activity worker lost; signal outcome may be uncertain",
                ))
            }) {
                Ok(result) => {
                    job.value["state"] = json!("succeeded");
                    job.value["result"] = result;
                }
                Err(error) => {
                    job.value["state"] = json!("failed");
                    job.value["error"] = crate::client::diagnostic(&error);
                }
            }
        }
        self.jobs
            .retain(|_, j| j.thread.is_some() || j.started.elapsed() < RETENTION);
    }
    pub fn active(&self) -> usize {
        self.jobs.values().filter(|j| j.thread.is_some()).count()
    }
    pub fn handle(
        &mut self,
        store: &Store,
        cell: Option<&Cell>,
        binding: &Binding,
        command: Command,
    ) -> Result<Value> {
        self.tick();
        if let Command::Poll { id } = command {
            let job = self
                .jobs
                .get(&id)
                .filter(|j| j.project == binding.project_id)
                .ok_or_else(|| missing("activity request in project (expired or restarted)"))?;
            authorize(store, binding, &job.target)?;
            if job.generation != store.generation() {
                return Err(conflict("activity generation changed"));
            }
            return Ok(job.value.clone());
        }
        let (target, signal) = match &command {
            Command::List { target, .. } => (target, None),
            Command::Cancel {
                target,
                snapshot,
                pid,
            } => (target, Some((*snapshot, *pid, false))),
            Command::Terminate {
                target,
                snapshot,
                pid,
            } => (target, Some((*snapshot, *pid, true))),
            Command::Poll { .. } => unreachable!(),
        };
        authorize(store, binding, target)?;
        let observation = if let Some((snapshot, pid, terminate)) = signal {
            let job = self
                .jobs
                .get(&snapshot)
                .filter(|j| {
                    j.project == binding.project_id
                        && j.target == *target
                        && j.generation == store.generation()
                })
                .ok_or_else(|| missing("fresh activity snapshot in branch"))?;
            if let Some((previous, id)) = job.signals.get(&pid) {
                if *previous != terminate {
                    return Err(conflict("session already signalled; refresh activity"));
                }
                return self.jobs.get(id).map(|j| j.value.clone()).ok_or_else(|| {
                    conflict("signal receipt expired; refresh activity before acting again")
                });
            }
            Some(observed(&job.value, pid, terminate)?)
        } else {
            None
        };
        if self.active() >= WORKERS {
            return Err(conflict("both activity workers are busy"));
        }
        if self.jobs.len() >= MAX_JOBS {
            let oldest = self
                .jobs
                .iter()
                .filter(|(id, j)| j.thread.is_none() && !signal.is_some_and(|(s, _, _)| s == **id))
                .map(|(id, j)| (*id, j.started))
                .min_by_key(|(_, t)| *t);
            if let Some((id, _)) = oldest {
                self.jobs.remove(&id);
            } else {
                return Err(conflict("activity request capacity reached"));
            }
        }
        let cell = cell.ok_or_else(|| conflict("engine is disabled"))?;
        let branch = store.branch(target.branch)?;
        if branch.endpoint.desired_state != supabricks_core::resource::DesiredState::Running
            || branch.revision != branch.observed_revision
            || !cell.connection_ready(store, &branch)?
        {
            return Err(conflict(
                "activity requires a running, ready compute; resume it explicitly",
            ));
        }
        let input = sql::Input {
            port: branch
                .ports
                .ok_or_else(|| conflict("compute ports unavailable"))?
                .sql,
            password: store.endpoint_password(branch.endpoint.id)?,
            include_query_text: matches!(
                command,
                Command::List {
                    include_query_text: true,
                    ..
                }
            ),
            observation,
            terminate: signal.is_some_and(|(_, _, t)| t),
        };
        let id = OperationId::new();
        let (tx, receiver) = mpsc::channel();
        let thread = thread::Builder::new()
            .name("postgres-activity".into())
            .spawn(move || {
                let _ = tx.send(sql::run(input));
            })?;
        let value = json!({"id":id,"state":"running","target":target,"generation":store.generation(),"expires_after_seconds":30});
        self.jobs.insert(
            id,
            Job {
                project: binding.project_id,
                target: target.clone(),
                generation: store.generation(),
                started: Instant::now(),
                thread: Some(thread),
                receiver,
                value: value.clone(),
                signals: HashMap::new(),
            },
        );
        if let Some((snapshot, pid, terminate)) = signal {
            self.jobs
                .get_mut(&snapshot)
                .unwrap()
                .signals
                .insert(pid, (terminate, id));
        }
        Ok(value)
    }
}
fn authorize(store: &Store, binding: &Binding, target: &Target) -> Result<()> {
    target.validate(store, binding)?;
    if store.governed_branch(target.branch)? {
        return Err(invalid(
            "live activity requires local operator authority; governed monitoring and session control are not enabled",
        ));
    }
    Ok(())
}
fn observed(value: &Value, pid: i32, terminate: bool) -> Result<Value> {
    if pid <= 0 {
        return Err(invalid("session PID must be positive"));
    }
    let result = &value["result"];
    let session = result["sessions"]
        .as_array()
        .and_then(|rows| rows.iter().find(|r| r["pid"] == pid))
        .ok_or_else(|| missing("session in completed activity snapshot"))?;
    let permission = if terminate {
        "can_terminate"
    } else {
        "can_cancel"
    };
    if session[permission] != true {
        return Err(conflict("session is protected or no longer cancellable"));
    }
    Ok(
        json!({"pid":pid,"backend_start":session["backend_start"],"query_start":session["query_start"],"postmaster_start":result["postmaster_start"]}),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn receipts_are_scoped_and_expire_and_governed_access_is_refused() {
        use crate::{
            operations::{Mutation, Ports},
            project::ProjectConfig,
        };
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut store = Store::open(dir.path()).unwrap();
        let project = ProjectConfig {
            format_version: 1,
            id: ProjectId::new(),
            name: "activity".into(),
        };
        store.register_project(&project).unwrap();
        let operation = store
            .submit(
                project.id,
                "main",
                Mutation::CreateBranch {
                    name: "main".into(),
                    parent_id: None,
                    ports: Ports {
                        sql: 5400,
                        external_http: 5401,
                        internal_http: 5402,
                    },
                },
            )
            .unwrap();
        while let Some(ticket) = store.ticket(operation.id).unwrap() {
            store.checkpoint(&ticket, json!({})).unwrap();
        }
        let target = Target {
            branch: operation.branch_id,
            revision: store.branch(operation.branch_id).unwrap().revision,
        };
        let mut binding = Binding {
            project_id: project.id,
            worktree: dir.path().into(),
        };
        let mut service = Service::default();
        let id = OperationId::new();
        let (_sender, receiver) = mpsc::channel();
        service.jobs.insert(
            id,
            Job {
                project: project.id,
                target: target.clone(),
                generation: store.generation(),
                started: Instant::now(),
                thread: None,
                receiver,
                value: json!({"id":id,"state":"succeeded"}),
                signals: HashMap::new(),
            },
        );
        assert_eq!(
            service
                .handle(&store, None, &binding, Command::Poll { id })
                .unwrap()["state"],
            "succeeded"
        );
        binding.project_id = ProjectId::new();
        assert!(
            service
                .handle(&store, None, &binding, Command::Poll { id })
                .is_err()
        );
        binding.project_id = project.id;
        let mut stale = target.clone();
        stale.revision -= 1;
        assert!(authorize(&store, &binding, &stale).is_err());
        let db = rusqlite::Connection::open(dir.path().join("state.sqlite3")).unwrap();
        db.execute(
            "INSERT INTO governed_branches VALUES (?1,'ready')",
            [target.branch.to_string()],
        )
        .unwrap();
        assert!(
            service
                .handle(&store, None, &binding, Command::Poll { id })
                .unwrap_err()
                .to_string()
                .contains("governed")
        );
        assert!(
            service
                .handle(
                    &store,
                    None,
                    &binding,
                    Command::List {
                        target,
                        include_query_text: true
                    }
                )
                .unwrap_err()
                .to_string()
                .contains("governed")
        );
        service.jobs.get_mut(&id).unwrap().started = Instant::now() - RETENTION;
        service.tick();
        assert!(service.jobs.is_empty());
    }
    #[test]
    fn signals_require_an_observed_eligible_session() {
        let mut v = json!({"result":{"postmaster_start":"start","sessions":[{"pid":42,"backend_start":"birth","query_start":"query","can_cancel":false,"can_terminate":true}]}});
        assert!(observed(&v, 0, true).is_err());
        assert!(observed(&v, 43, true).is_err());
        assert!(observed(&v, 42, false).is_err());
        assert_eq!(observed(&v, 42, true).unwrap()["backend_start"], "birth");
        v["result"]["sessions"][0]["can_terminate"] = json!(false);
        assert!(observed(&v, 42, true).is_err());
    }
    #[test]
    fn commands_reject_arbitrary_signal_or_sql_fields() {
        assert!(
            serde_json::from_value::<Command>(
                json!({"action":"poll","id":OperationId::new(),"sql":"SELECT 1"})
            )
            .is_err()
        );
    }
}
