//! Explicit browser workspace commands, dispatched by the sole daemon writer.
use crate::{
    api::{Action, Binding},
    client,
    query::Query,
    store::{
        Result, Store,
        error::{conflict, invalid, missing},
    },
    supervisor,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    fs,
    path::Path,
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
use supabricks_core::resource::{BranchId, DesiredState, OperationId};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub branch: BranchId,
    pub revision: i64,
}
impl Target {
    pub fn validate(&self, store: &Store, binding: &Binding) -> Result<()> {
        store.branch_in_project(binding.project_id, self.branch)?;
        let b = store.branch(self.branch)?;
        if b.revision != self.revision {
            return Err(conflict(
                "Branch changed; refresh and explicitly rebind before submitting work",
            ));
        }
        store.accepting_work(self.branch)
    }
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    NotebookRefresh {
        target: Target,
        key: String,
    },
    NotebookRefreshStatus {
        id: OperationId,
    },
    NotebookCancelRefresh {
        id: OperationId,
    },
    Notebook {
        command: crate::notebooks::contract::Command,
    },
    Environment {
        command: crate::environments::Command,
    },
    NotebookFiles {
        command: crate::notebooks::files::Command,
    },
    Ingest {
        command: super::ingestion::Command,
    },
    CreateDatabase {
        name: String,
        key: String,
    },
    CreateBranch {
        name: String,
        target: Target,
        key: String,
    },
    SetState {
        target: Target,
        desired: DesiredState,
        key: String,
    },
    DeleteBranch {
        target: Target,
        key: String,
    },
    Operation {
        id: OperationId,
    },
    Connect {
        target: Target,
    },
    Query {
        id: OperationId,
        target: Target,
        sql: String,
        read_only: bool,
        max_rows: usize,
        timeout_ms: u64,
    },
    Catalog {
        id: OperationId,
        target: Target,
    },
    Preview {
        id: OperationId,
        target: Target,
        schema: String,
        table: String,
    },
    QueryStatus {
        id: OperationId,
    },
    CancelQuery {
        id: OperationId,
    },
    SavedList,
    SavedGet {
        id: OperationId,
    },
    SavedPut {
        id: OperationId,
        expected_revision: i64,
        target: Target,
        title: String,
        sql: String,
    },
    SavedDelete {
        id: OperationId,
        expected_revision: i64,
    },
}
impl Command {
    pub fn mutation(self, store: &Store, binding: &Binding) -> Result<Action> {
        Ok(match self {
            Self::CreateDatabase { name, key } => Action::CreateDatabase { name, key },
            Self::CreateBranch { name, target, key } => {
                target.validate(store, binding)?;
                Action::CreateBranch {
                    name,
                    parent: target.branch.to_string(),
                    key,
                    point: Default::default(),
                }
            }
            Self::SetState {
                target,
                desired,
                key,
            } => {
                target.validate(store, binding)?;
                Action::SetState {
                    branch: target.branch.to_string(),
                    expected_revision: target.revision,
                    desired,
                    key,
                }
            }
            Self::DeleteBranch { target, key } => {
                target.validate(store, binding)?;
                Action::DeleteBranch {
                    branch: target.branch.to_string(),
                    expected_revision: target.revision,
                    key,
                    force: false,
                }
            }
            Self::Operation { id } => Action::GetOperation { id },
            _ => return Err(invalid("unsupported workspace command")),
        })
    }
}
pub fn identifier(s: &str) -> Result<String> {
    if s.is_empty() || s.len() > 63 || s.contains('\0') {
        return Err(invalid("invalid PostgreSQL identifier"));
    }
    Ok(format!("\"{}\"", s.replace('"', "\"\"")))
}

struct Work {
    scope: String,
    target: Target,
    query: Query,
    started: Instant,
    finished: Option<Instant>,
    cancel: tokio::sync::watch::Sender<bool>,
    receiver: mpsc::Receiver<Result<Value>>,
    thread: Option<thread::JoinHandle<()>>,
    value: Value,
}
#[derive(Default)]
pub(crate) struct Queries {
    work: HashMap<String, Work>,
}
impl Queries {
    pub fn tick(&mut self) {
        for w in self.work.values_mut() {
            if w.finished.is_some() {
                continue;
            }
            let result = match w.receiver.try_recv() {
                Ok(result) => Some(result),
                Err(mpsc::TryRecvError::Disconnected) => Some(Err(conflict(
                    "Query worker was lost; write outcome is unknown. Inspect before retrying",
                ))),
                Err(mpsc::TryRecvError::Empty) => None,
            };
            if let Some(result) = result {
                w.value["elapsed_ms"] = json!(w.started.elapsed().as_millis() as u64);
                match result {
                    Ok(value) => {
                        w.value["state"] = json!("succeeded");
                        w.value["result"] = value;
                    }
                    Err(error) => {
                        let cancelled = *w.cancel.borrow()
                            && matches!(&error, crate::store::Error::Operation(supabricks_core::error::OperationError::Query{sqlstate,..}) if sqlstate=="57014");
                        w.value["state"] = json!(if cancelled { "cancelled" } else { "failed" });
                        w.value["error"] = client::diagnostic(&error);
                    }
                }
                w.finished = Some(Instant::now());
                if let Some(t) = w.thread.take() {
                    let _ = t.join();
                }
            }
        }
        self.work.retain(|_, w| {
            w.finished
                .is_none_or(|t| t.elapsed() < Duration::from_secs(600))
        });
    }
    pub fn active(&self) -> usize {
        self.work.values().filter(|w| w.finished.is_none()).count()
    }
    pub fn existing(
        &self,
        scope: &str,
        id: OperationId,
        target: &Target,
        query: &Query,
    ) -> Result<Option<Value>> {
        match self.work.get(&id.to_string()) {
            Some(w) if w.scope == scope && &w.target == target && &w.query == query => {
                Ok(Some(w.value.clone()))
            }
            Some(_) => Err(conflict("query identity already used; inspect its status")),
            None => Ok(None),
        }
    }
    pub fn start(
        &mut self,
        scope: String,
        id: OperationId,
        target: Target,
        query: Query,
        connection: Value,
        generation: i64,
    ) -> Result<Value> {
        if self.work.len() >= 32 {
            let oldest = self
                .work
                .iter()
                .filter_map(|(id, w)| w.finished.map(|t| (id.clone(), t)))
                .min_by_key(|(_, t)| *t)
                .map(|(id, _)| id);
            if let Some(id) = oldest {
                self.work.remove(&id);
            } else {
                return Err(conflict("query handle capacity reached"));
            }
        }
        let (cancel, rx) = tokio::sync::watch::channel(false);
        let (tx, receiver) = mpsc::channel();
        let execute = query.clone();
        let thread = thread::Builder::new()
            .name(format!("console-query-{id}"))
            .spawn(move || {
                let _ = tx.send(execute.run_cancellable(connection, rx));
            })?;
        let value = json!({"id":id,"generation":generation,"target":target,"state":"running","read_only":query.read_only,"elapsed_ms":0});
        self.work.insert(
            id.to_string(),
            Work {
                scope,
                target,
                query,
                started: Instant::now(),
                finished: None,
                cancel,
                receiver,
                thread: Some(thread),
                value: value.clone(),
            },
        );
        Ok(value)
    }
    pub fn status(&mut self, scope: &str, id: OperationId, cancel: bool) -> Result<Value> {
        self.tick();
        let w=self.work.get_mut(&id.to_string()).filter(|w|w.scope==scope).ok_or_else(||missing("Query handle expired or belongs to another session/generation; an interrupted write may have committed. Never replay automatically"))?;
        if cancel && w.finished.is_none() {
            let _ = w.cancel.send(true);
            w.value["state"] = json!("cancelling");
        }
        let mut value = w.value.clone();
        if w.finished.is_none() {
            value["elapsed_ms"] = json!(w.started.elapsed().as_millis() as u64);
        }
        Ok(value)
    }
    pub fn cancel_all(&self) {
        for w in self.work.values().filter(|w| w.finished.is_none()) {
            let _ = w.cancel.send(true);
        }
    }
}
impl Drop for Queries {
    fn drop(&mut self) {
        self.cancel_all();
        for w in self.work.values_mut() {
            if let Some(t) = w.thread.take() {
                let _ = t.join();
            }
        }
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Saved {
    version: u32,
    id: OperationId,
    revision: i64,
    target: Target,
    title: String,
    sql: String,
}
fn saved_dir(root: &Path, binding: &Binding) -> Result<std::path::PathBuf> {
    let base = root.join("queries");
    super::directory(&base)?;
    let project = base.join(binding.project_id.to_string());
    super::directory(&project)?;
    fs::File::open(root)?.sync_all()?;
    fs::File::open(&base)?.sync_all()?;
    Ok(project)
}
fn read_saved(path: &Path) -> Result<Saved> {
    use std::os::unix::fs::MetadataExt;
    let m = fs::symlink_metadata(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            missing("saved query in project")
        } else {
            e.into()
        }
    })?;
    if !m.is_file()
        || m.size() > 65536
        || m.mode() & 0o077 != 0
        || m.uid() != unsafe { libc::geteuid() }
    {
        return Err(invalid(
            "Saved query must be a bounded private regular file",
        ));
    }
    let saved: Saved = serde_json::from_slice(&fs::read(path)?)?;
    if saved.version != 1 {
        return Err(invalid("unsupported saved query format"));
    }
    Ok(saved)
}
pub fn saved(store: &Store, binding: &Binding, command: Command) -> Result<Value> {
    let directory = saved_dir(store.root(), binding)?;
    match command {
        Command::SavedList => {
            let mut values = Vec::new();
            for entry in fs::read_dir(directory)? {
                let entry = entry?;
                if entry.path().extension().is_none_or(|e| e != "json") {
                    continue;
                }
                if values.len() >= 100 {
                    return Err(conflict("saved query limit exceeded"));
                }
                let s = read_saved(&entry.path())?;
                values.push(
                    json!({"id":s.id,"revision":s.revision,"target":s.target,"title":s.title}),
                );
            }
            Ok(json!({"queries":values}))
        }
        Command::SavedGet { id } => Ok(json!(read_saved(&directory.join(format!("{id}.json")))?)),
        Command::SavedPut {
            id,
            expected_revision,
            target,
            title,
            sql,
        } => {
            target.validate(store, binding)?;
            Query {
                sql: sql.clone(),
                read_only: true,
                max_rows: 200,
                timeout_ms: 10000,
            }
            .validate()?;
            if title.trim().is_empty() || title.len() > 120 || title.chars().any(char::is_control) {
                return Err(invalid(
                    "saved query title requires 1–120 bytes without control characters",
                ));
            }
            let path = directory.join(format!("{id}.json"));
            let old = if path.try_exists()? {
                read_saved(&path)?.revision
            } else {
                0
            };
            if old != expected_revision {
                return Err(conflict("saved query changed; reload before saving"));
            }
            if old == 0 && fs::read_dir(&directory)?.count() >= 100 {
                return Err(conflict("project limit of 100 saved queries reached"));
            }
            let value = Saved {
                version: 1,
                id,
                revision: old + 1,
                target,
                title,
                sql,
            };
            supervisor::write_json(&path, &value)?;
            Ok(json!(value))
        }
        Command::SavedDelete {
            id,
            expected_revision,
        } => {
            let path = directory.join(format!("{id}.json"));
            if read_saved(&path)?.revision != expected_revision {
                return Err(conflict("saved query changed; reload before deleting"));
            }
            fs::remove_file(path)?;
            fs::File::open(directory)?.sync_all()?;
            Ok(json!({"deleted":true}))
        }
        _ => Err(invalid("unsupported saved query command")),
    }
}

/// Only the fields used by the browser; internal effect results stay private.
pub fn operation(value: Value) -> Value {
    json!({"id":value["id"],"branch_id":value["branch_id"],"revision":value["revision"],"status":value["status"],"next_step":value["next_step"],"steps":value["steps"],
        "error":value.get("error").filter(|e|!e.is_null()).map(|e|json!({"code":e["code"],"message":e["message"].as_str().unwrap_or("Operation failed; inspect doctor").chars().take(512).collect::<String>()}))})
}
