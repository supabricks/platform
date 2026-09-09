//! Daemon-owned Jupyter servers and generation-fenced, ephemeral notebook handles.
//! Durable native process evidence and A03 references fence all execution. Handles
//! are intentionally lost across daemon restart; no code or live endpoint is restored.
pub mod contract;
mod runtime;
use crate::{
    api::Binding,
    console::workspace::Target,
    sessions::Sessions,
    store::{
        Result, Store,
        error::{conflict, invalid, missing},
    },
    supervisor::{self, Launch},
};
use contract::{Command, Limits, Transport};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
    process::Child,
};
use supabricks_core::resource::OperationId;

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
fn active(state: &str) -> bool {
    matches!(
        state,
        "starting" | "ready" | "busy" | "interrupting" | "stopping"
    )
}
struct Entry {
    id: OperationId,
    binding: Binding,
    owner: String,
    key: String,
    target: Target,
    limits: Limits,
    generation: u64,
    state: String,
    error: Option<String>,
    session: Option<OperationId>,
    kernel: Option<OperationId>,
    launch: Option<Launch>,
    started: i64,
    expires: i64,
    activity: i64,
    connected: i64,
    interrupt_at: Option<i64>,
    stop_reason: Option<String>,
    restart: bool,
    actions: BTreeMap<String, Value>,
}
impl Entry {
    fn view(&self, store: &Store) -> Result<Value> {
        let session = self
            .session
            .map(|id| store.analytical_session(self.binding.project_id, id))
            .transpose()?;
        Ok(
            json!({"id":self.id,"generation":self.generation,"daemon_generation":store.generation(),
            "project_id":self.binding.project_id,"branch_id":self.target.branch,"worktree":self.binding.worktree,
            "state":self.state,"error":self.error,"kernel_id":self.kernel,"session_id":self.session,
            "epoch_id":session.as_ref().and_then(|s|s.epoch_id),"epoch":session.as_ref().and_then(|s|s.metadata.clone()),
            "expires_at_ms":self.expires,"limits":self.limits,"protocol_version":contract::PROTOCOL,
            "execution_replayed":false}),
        )
    }
}
struct Server {
    role: String,
    dir: PathBuf,
    token: String,
    child: Child,
    started: i64,
    idle_since: i64,
    port: Option<u16>,
}
#[derive(Default)]
pub struct Notebooks {
    entries: HashMap<OperationId, Entry>,
    servers: BTreeMap<PathBuf, Server>,
    owners: BTreeMap<String, i64>,
    pub last_error: Option<String>,
}
impl Notebooks {
    pub fn recover(store: &mut Store) -> Result<Self> {
        // Kill servers first: no kernel launch may race reconciliation. Gated
        // children without a committed record fail when their daemon socket dies.
        for prefix in ["notebook-server-", "notebook-kernel-"] {
            for record in store
                .native_processes()?
                .into_iter()
                .filter(|p| p.role.starts_with(prefix))
            {
                if record.root != store.root() {
                    return Err(conflict("notebook process belongs to a different root"));
                }
                supervisor::stop(&record)?;
                store.forget_native_process(&record)?;
            }
        }
        let dir = store.root().join("notebook-work");
        if dir.exists() {
            std::fs::remove_dir_all(dir)?;
        }
        // Sessions::recover closes even waiting notebook admissions, including
        // a crash between A03 admission and creating the in-memory handle.
        Ok(Self::default())
    }
    pub fn heartbeat(&mut self, instance: &str, sessions: &[String]) -> Result<()> {
        if sessions.len() > 32
            || sessions
                .iter()
                .any(|s| s.len() != 64 || !s.bytes().all(|b| b.is_ascii_hexdigit()))
        {
            return Err(invalid("invalid notebook owner heartbeat"));
        }
        let prefix = format!("{instance}:");
        self.owners.retain(|owner, _| !owner.starts_with(&prefix));
        for session in sessions {
            self.owners
                .insert(format!("{prefix}{session}"), now() + 6000);
        }
        Ok(())
    }
    pub fn handle(
        &mut self,
        store: &mut Store,
        cell: Option<&crate::engine::Cell>,
        binding: &Binding,
        owner: &str,
        command: Command,
    ) -> Result<Value> {
        match command {
            Command::Create {
                key,
                target,
                limits,
            } => {
                contract::key(&key)?;
                limits.validate()?;
                target.validate(store, binding)?;
                if let Some(e) = self.entries.values().find(|e| {
                    e.owner == owner && e.binding.worktree == binding.worktree && e.key == key
                }) {
                    if e.target != target || e.limits != limits {
                        return Err(conflict("notebook key was used with different parameters"));
                    }
                    return e.view(store);
                }
                if self.entries.len() >= 128 {
                    return Err(conflict(
                        "notebook handle limit reached; sign out to release old handles",
                    ));
                }
                runtime::worker(store)?;
                let id = OperationId::new();
                let e = Entry {
                    id,
                    binding: binding.clone(),
                    owner: owner.into(),
                    key,
                    target,
                    limits,
                    generation: 0,
                    state: "stopped".into(),
                    error: None,
                    session: None,
                    kernel: None,
                    launch: None,
                    started: 0,
                    expires: 0,
                    activity: now(),
                    connected: 0,
                    interrupt_at: None,
                    stop_reason: None,
                    restart: false,
                    actions: Default::default(),
                };
                let result = e.view(store)?;
                self.entries.insert(id, e);
                self.owners.entry(owner.into()).or_insert(now() + 6000);
                Ok(result)
            }
            Command::List => Ok(json!(
                self.entries
                    .values()
                    .filter(|e| e.owner == owner
                        && e.binding.worktree == binding.worktree
                        && e.binding.project_id == binding.project_id)
                    .map(|e| e.view(store))
                    .collect::<Result<Vec<_>>>()?
            )),
            Command::Status { id, generation } => {
                self.owned(binding, owner, id, generation)?.view(store)
            }
            command => {
                let (id, generation, key) = match &command {
                    Command::Start {
                        id,
                        generation,
                        key,
                    }
                    | Command::Interrupt {
                        id,
                        generation,
                        key,
                    }
                    | Command::Restart {
                        id,
                        generation,
                        key,
                    }
                    | Command::Shutdown {
                        id,
                        generation,
                        key,
                    } => (*id, *generation, key.clone()),
                    _ => unreachable!(),
                };
                contract::key(&key)?;
                // Validate scope before looking up idempotency keys. A retry of
                // the same action is harmless even after it advanced generation.
                let e = self.entries.get(&id).ok_or_else(|| {
                    missing("notebook handle; restart explicitly after daemon recovery")
                })?;
                Self::scope(e, binding, owner)?;
                let request = serde_json::to_value(&command)?;
                if let Some(previous) = e.actions.get(&key) {
                    if previous != &request {
                        return Err(conflict("notebook action key has different parameters"));
                    }
                    return e.view(store);
                }
                self.owned(binding, owner, id, generation)?;
                if e.actions.len() >= 128 {
                    return Err(conflict(
                        "notebook action history is full; create a new handle",
                    ));
                }
                let mut e = self.entries.remove(&id).unwrap();
                let result = (|| {
                    match command {
                        Command::Start { .. } => {
                            if active(&e.state) {
                                return Err(conflict(
                                    "notebook already active; use restart explicitly",
                                ));
                            }
                            self.start(store, cell, &mut e)?;
                        }
                        Command::Restart { .. } => {
                            if e.state == "stopping" {
                                return Err(conflict("notebook cleanup is still pending"));
                            }
                            e.target.validate(store, binding)?;
                            e.restart = true;
                            e.stop_reason = Some("restart".into());
                            e.state = "stopping".into();
                        }
                        Command::Shutdown { .. } => {
                            e.restart = false;
                            e.stop_reason = Some("stopped".into());
                            e.state = "stopping".into();
                        }
                        Command::Interrupt { .. } => {
                            if !matches!(e.state.as_str(), "ready" | "busy") {
                                return Err(conflict("kernel is not available for interrupt"));
                            }
                            self.control(&e, "interrupt")?;
                            e.interrupt_at = Some(now());
                            e.state = "interrupting".into();
                        }
                        _ => unreachable!(),
                    }
                    e.actions.insert(key, request);
                    e.view(store)
                })();
                self.entries.insert(id, e);
                result
            }
        }
    }
    fn scope(e: &Entry, binding: &Binding, owner: &str) -> Result<()> {
        if e.owner != owner
            || e.binding.project_id != binding.project_id
            || e.binding.worktree != binding.worktree
        {
            return Err(missing("notebook in this console session"));
        }
        Ok(())
    }
    fn owned(
        &self,
        binding: &Binding,
        owner: &str,
        id: OperationId,
        generation: u64,
    ) -> Result<&Entry> {
        let e = self
            .entries
            .get(&id)
            .ok_or_else(|| missing("notebook handle; execution is lost after daemon restart"))?;
        Self::scope(e, binding, owner)?;
        if e.generation != generation {
            return Err(conflict(
                "notebook generation changed; never replay pending cells",
            ));
        }
        Ok(e)
    }
    pub fn transport(
        &mut self,
        store: &Store,
        binding: &Binding,
        owner: &str,
        event: Transport,
    ) -> Result<Value> {
        if matches!(event, Transport::Revoke) {
            self.owners.remove(owner);
            for e in self
                .entries
                .values_mut()
                .filter(|e| e.owner == owner && e.binding.worktree == binding.worktree)
            {
                e.restart = false;
                e.state = "stopping".into();
                e.stop_reason = Some("session_revoked".into());
            }
            return Ok(json!({"revoked":true}));
        }
        let (id, generation) = match event {
            Transport::Connect { id, generation } | Transport::Check { id, generation } => {
                (id, generation)
            }
            _ => unreachable!(),
        };
        let e = self.owned(binding, owner, id, generation)?;
        if self.owners.get(owner).is_none_or(|t| *t <= now())
            || !matches!(e.state.as_str(), "ready" | "busy" | "interrupting")
            || now() >= e.expires
        {
            return Err(conflict("notebook execution context is unavailable"));
        }
        let session = store.analytical_session(binding.project_id, e.session.unwrap())?;
        if session.state != "ready" {
            return Err(conflict("Spark context is lost; restart explicitly"));
        }
        let server = self
            .servers
            .get(&binding.worktree)
            .ok_or_else(|| conflict("notebook server lost"))?;
        if matches!(event, Transport::Check { .. }) {
            let state = e.state.clone();
            self.entries.get_mut(&id).unwrap().connected = now();
            return Ok(json!({"state":state,"generation":generation}));
        }
        // This private IPC result must never be returned to a browser.
        let value = json!({"port":server.port.ok_or_else(||conflict("server not ready"))?,"token":server.token,"kernel_id":e.kernel,"generation":generation});
        self.entries.get_mut(&id).unwrap().connected = now();
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use supabricks_core::resource::{BranchId, ProjectId};

    #[test]
    fn ownership_requires_project_worktree_session_and_current_generation() {
        let binding = Binding {
            project_id: ProjectId::new(),
            worktree: "/tmp/notebook-a".into(),
        };
        let id = OperationId::new();
        let mut notebooks = Notebooks::default();
        let owner = format!("console:{}", "a".repeat(64));
        notebooks.entries.insert(
            id,
            Entry {
                id,
                binding: binding.clone(),
                owner: owner.clone(),
                key: "create".into(),
                target: Target {
                    branch: BranchId::new(),
                    revision: 1,
                },
                limits: Limits::default(),
                generation: 2,
                state: "ready".into(),
                error: None,
                session: None,
                kernel: None,
                launch: None,
                started: 0,
                expires: 0,
                activity: 0,
                connected: 0,
                interrupt_at: None,
                stop_reason: None,
                restart: false,
                actions: Default::default(),
            },
        );
        assert!(notebooks.owned(&binding, &owner, id, 2).is_ok());
        assert!(notebooks.owned(&binding, &owner, id, 1).is_err());
        assert!(notebooks.owned(&binding, "another-session", id, 2).is_err());
        let mut other = binding.clone();
        other.project_id = ProjectId::new();
        assert!(notebooks.owned(&other, &owner, id, 2).is_err());
        other = binding.clone();
        other.worktree = "/tmp/notebook-b".into();
        assert!(notebooks.owned(&other, &owner, id, 2).is_err());
        notebooks.heartbeat("console", &["a".repeat(64)]).unwrap();
        assert!(notebooks.owners.contains_key(&owner));
        notebooks.heartbeat("console", &[]).unwrap();
        assert!(!notebooks.owners.contains_key(&owner));
        assert!(notebooks.heartbeat("console", &["invalid".into()]).is_err());
    }
}
