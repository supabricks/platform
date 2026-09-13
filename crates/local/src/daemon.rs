//! Private, versioned local control socket. The daemon alone owns Store.
//! The daemon journals intent and authorizes native effects before execution.
use crate::store::error::{conflict, invalid};
use crate::{
    operations::Mutation,
    project::ProjectConfig,
    store::{Error, Result, SCHEMA_VERSION, Store},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{FileTypeExt, PermissionsExt},
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    time::Duration,
};
use supabricks_core::resource::{BranchId, OperationId, ProjectId};
const LIMIT: u64 = 64 * 1024;

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope {
    pub version: u32,
    pub request: Request,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "method", rename_all = "snake_case", deny_unknown_fields)]
pub enum Request {
    NotebookTransport {
        binding: crate::api::Binding,
        generation: i64,
        owner: String,
        event: crate::notebooks::contract::Transport,
    },
    NotebookHeartbeat {
        binding: crate::api::Binding,
        generation: i64,
        instance: String,
        sessions: Vec<String>,
    },
    ConsoleAction {
        binding: crate::api::Binding,
        generation: i64,
        owner: String,
        action: crate::console::workspace::Command,
    },
    ConsoleOpen {
        binding: crate::api::Binding,
        assets: PathBuf,
    },
    ConsoleOverview {
        binding: crate::api::Binding,
        generation: i64,
    },
    Api {
        api_version: u32,
        binding: crate::api::Binding,
        action: crate::api::Action,
    },
    Status,
    RegisterProject {
        config: ProjectConfig,
    },
    Submit {
        project_id: ProjectId,
        key: String,
        mutation: Mutation,
    },
    Operation {
        id: OperationId,
    },
    Pending,
    Branch {
        id: BranchId,
    },
    RenameBranch {
        project_id: ProjectId,
        id: BranchId,
        name: String,
    },
    SelectWorktree {
        path: PathBuf,
        project_id: ProjectId,
        branch_id: BranchId,
    },
    Selection {
        path: PathBuf,
        project_id: ProjectId,
    },
    ListBranches {
        project_id: ProjectId,
        #[serde(default)]
        include_deleted: bool,
    },
    GetBranch {
        project_id: ProjectId,
        id: BranchId,
    },
    Connection {
        project_id: ProjectId,
        id: BranchId,
    },
    AcquireLease {
        project_id: ProjectId,
        branch_id: BranchId,
        holder: String,
        ttl_ms: u64,
    },
    ReleaseLease {
        project_id: ProjectId,
        lease: crate::store::Lease,
    },
    RenewLease {
        project_id: ProjectId,
        lease: crate::store::Lease,
        ttl_ms: u64,
    },
    Shutdown,
    AuthorizeProcess {
        role: String,
        generation: i64,
        token: String,
        pid: u32,
    },
}

pub struct Daemon {
    environments: crate::environments::Manager,
    notebooks: crate::notebooks::Notebooks,
    consoles: crate::console::Consoles,
    console_queries: crate::console::workspace::Queries,
    publisher: crate::analytics::Publisher,
    sessions: crate::sessions::Sessions,
    store: Store,
    listener: UnixListener,
    socket: PathBuf,
    cell: Option<crate::engine::Cell>,
    validator: Option<crate::engine::validation::Validator>,
    gateway: Option<crate::connections::Gateway>,
    queries: Vec<std::thread::JoinHandle<()>>,
    ingest_error: Option<String>,
    ingestion: crate::ingest::service::Service,
    uploads: crate::console::ingestion::Uploads,
}
impl Daemon {
    pub fn bind(root: &Path) -> Result<Self> {
        // Acquire ownership before touching a stale socket or migrating state.
        let mut store = Store::open(root)?;
        let notebooks = crate::notebooks::Notebooks::recover(&mut store)?;
        let environments = crate::environments::Manager::recover(&mut store)?;
        let consoles = crate::console::Consoles::recover(&mut store)?;
        crate::ingest::recover(&mut store)?;
        let socket = store.root().join("control.sock");
        match fs::symlink_metadata(&socket) {
            Ok(meta) if meta.file_type().is_socket() => fs::remove_file(&socket)?,
            Ok(_) => return Err(conflict("control socket path contains a non-socket file")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
        let listener = UnixListener::bind(&socket)?;
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
        let cell = if store.root().join("runtime.json").exists() {
            Some(crate::engine::Cell::open(&mut store)?)
        } else {
            None
        };
        let validator = if cell.is_some() {
            Some(crate::engine::validation::Validator::bind(&store)?)
        } else {
            None
        };
        let gateway = cell
            .as_ref()
            .map(|c| crate::connections::Gateway::new(&mut store, c.connection_timeout()))
            .transpose()?;
        let sessions = crate::sessions::Sessions::recover(&mut store)?;
        let publisher = crate::analytics::Publisher::recover(&mut store)?;
        Ok(Self {
            environments,
            notebooks,
            consoles,
            console_queries: Default::default(),
            publisher,
            sessions,
            queries: Vec::new(),
            ingest_error: None,
            ingestion: Default::default(),
            uploads: Default::default(),
            gateway,
            validator,
            store,
            listener,
            socket,
            cell,
        })
    }
    pub fn enable_engine(mut self, bundle: &Path, helpers: &Path) -> Result<Self> {
        if self.cell.is_none() {
            crate::engine::RuntimeConfig::initialize(&self.store, bundle, helpers)?;
            self.cell = Some(crate::engine::Cell::open(&mut self.store)?);
            self.validator = Some(crate::engine::validation::Validator::bind(&self.store)?);
            self.gateway = Some(crate::connections::Gateway::new(
                &mut self.store,
                self.cell.as_ref().unwrap().connection_timeout(),
            )?);
        }
        Ok(self)
    }
    pub fn serve(mut self) -> Result<()> {
        self.listener.set_nonblocking(true)?;
        let mut next_tick = std::time::Instant::now();
        let mut stopping = false;
        loop {
            self.queries.retain(|t| !t.is_finished());
            if stopping {
                self.console_queries.cancel_all();
            }
            self.console_queries.tick();
            if let (Some(gateway), Some(cell)) = (&mut self.gateway, &self.cell) {
                if stopping {
                    if !gateway.stop(&mut self.store)? {
                        std::thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                } else {
                    gateway.tick(&mut self.store, cell)?;
                }
            }
            if std::time::Instant::now() >= next_tick {
                let environments_stopped = match self.environments.tick(&mut self.store, stopping) {
                    Ok(done) => {
                        self.environments.last_error = None;
                        done
                    }
                    Err(error) => {
                        self.environments.last_error = Some(error.to_string());
                        false
                    }
                };
                self.uploads.tick(&mut self.store, stopping)?;
                let ingestion_stopped = match self.ingestion.tick(&mut self.store, stopping) {
                    Ok(done) => {
                        self.ingest_error = None;
                        done
                    }
                    Err(e) => {
                        self.ingest_error = Some(e.to_string());
                        false
                    }
                };
                let notebooks_stopped = match self.notebooks.tick(
                    &mut self.store,
                    &mut self.sessions,
                    &mut self.environments,
                    self.cell.as_ref(),
                    stopping,
                ) {
                    Ok(()) => {
                        self.notebooks.last_error = None;
                        true
                    }
                    Err(error) => {
                        self.notebooks.last_error = Some(error.to_string());
                        false
                    }
                };
                let console_result = if stopping {
                    self.consoles.stop(&mut self.store)
                } else {
                    self.consoles.tick(&mut self.store)
                };
                self.consoles.last_error = console_result.err().map(|e| e.to_string());
                if !stopping {
                    self.sessions.last_error = self
                        .sessions
                        .tick(&mut self.store)
                        .err()
                        .map(|e| e.to_string());
                    self.publisher.last_error = self
                        .publisher
                        .tick(&mut self.store)
                        .err()
                        .map(|e| e.to_string());
                }
                let analytical_stopped = if stopping && notebooks_stopped {
                    match self.sessions.stop(&mut self.store) {
                        Ok(()) => {
                            self.sessions.last_error = None;
                            true
                        }
                        Err(error) => {
                            self.sessions.last_error = Some(error.to_string());
                            false
                        }
                    }
                } else {
                    true
                };
                if let Some(validator) = &self.validator {
                    validator.refresh(&self.store)?;
                }
                if let Some(cell) = &mut self.cell {
                    if stopping
                        && environments_stopped
                        && analytical_stopped
                        && notebooks_stopped
                        && self.consoles.last_error.is_none()
                        && self.ingest_error.is_none()
                        && ingestion_stopped
                    {
                        match cell.stop(&mut self.store) {
                            Ok(true) => return Ok(()),
                            Ok(false) => cell.last_error = None,
                            Err(e) => {
                                // Keep the lock/socket until every owned writer is gone.
                                // Exiting here makes socket disappearance look like a
                                // successful shutdown even though cleanup is incomplete.
                                let detail = e.to_string();
                                if cell.last_error.as_ref() != Some(&detail) {
                                    eprintln!("shutdown cleanup pending: {detail}");
                                }
                                cell.last_error = Some(detail);
                            }
                        }
                    } else if !stopping || !ingestion_stopped {
                        match cell.tick(&mut self.store) {
                            Ok(()) => cell.last_error = None,
                            Err(e) => cell.last_error = Some(e.to_string()),
                        }
                    }
                } else if stopping
                    && environments_stopped
                    && analytical_stopped
                    && notebooks_stopped
                    && self.consoles.last_error.is_none()
                    && self.ingest_error.is_none()
                    && ingestion_stopped
                {
                    return Ok(());
                }
                next_tick = std::time::Instant::now() + Duration::from_millis(200);
            }
            let (mut stream, _) = match self.listener.accept() {
                Ok(pair) => pair,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    // Wake immediately for streamed upload chunks while retaining
                    // the 20 ms ceiling for gateways and lifecycle maintenance.
                    use std::os::fd::AsRawFd;
                    let mut fd = libc::pollfd {
                        fd: self.listener.as_raw_fd(),
                        events: libc::POLLIN,
                        revents: 0,
                    };
                    let result = unsafe { libc::poll(&mut fd, 1, 20) };
                    if result < 0 {
                        let error = std::io::Error::last_os_error();
                        if error.kind() != std::io::ErrorKind::Interrupted {
                            return Err(error.into());
                        }
                    }
                    continue;
                }
                Err(e) => return Err(e.into()),
            };
            // A probe may close immediately after connect. Socket setup errors
            // belong to that client, not to the daemon's ownership lifetime.
            // BSD/macOS accept inherits the listener's O_NONBLOCK flag;
            // Linux does not. Request framing uses bounded blocking I/O.
            if stream.set_nonblocking(false).is_err()
                || stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .is_err()
                || stream
                    .set_write_timeout(Some(Duration::from_secs(2)))
                    .is_err()
            {
                continue;
            }
            let request = read_request(&mut stream);
            let shutdown = matches!(
                &request,
                Ok(Envelope {
                    version: 1,
                    request: Request::Shutdown
                })
            );
            let result = request.and_then(|envelope| {
                if envelope.version != 1 {
                    return Err(invalid("unsupported local API version"));
                }
                if stopping && !matches!(envelope.request, Request::Status | Request::Shutdown) {
                    return Err(conflict("daemon is stopping"));
                }
                if let Request::Api {
                    api_version,
                    binding,
                    action,
                } = envelope.request
                {
                    if api_version != crate::api::VERSION {
                        return Err(invalid("unsupported application API version"));
                    }
                    binding.validate(&mut self.store)?;
                    let (branch, query) = match action {
                        crate::api::Action::Sql {
                            branch,
                            sql,
                            read_only,
                            max_rows,
                            timeout_ms,
                        } => (
                            branch,
                            crate::query::Query {
                                sql,
                                read_only,
                                max_rows,
                                timeout_ms,
                            },
                        ),
                        crate::api::Action::Catalog { branch } => {
                            (branch, crate::query::Query::catalog())
                        }
                        other => return self.public_action(binding, other).map(Some),
                    };
                    query.validate()?;
                    if !query.read_only && branch.is_none() {
                        return Err(invalid("SQL writes require an explicit branch"));
                    }
                    if self.queries.len() + self.console_queries.active() >= crate::query::WORKERS {
                        return Err(supabricks_core::error::OperationError::Unavailable(
                            "all four SQL workers are busy".into(),
                        )
                        .into());
                    }
                    let id = crate::api::resolve(&self.store, &binding, branch.as_deref())?;
                    let target = self.handle(Request::Connection {
                        project_id: binding.project_id,
                        id,
                    })?;
                    let mut reply = stream.try_clone()?;
                    self.queries
                        .push(std::thread::Builder::new().name("app-sql".into()).spawn(
                            move || {
                                let response = response(query.run(target));
                                let _ = writeln!(reply, "{response}");
                            },
                        )?);
                    return Ok(None);
                }
                self.handle(envelope.request).map(Some)
            });
            if matches!(result, Ok(None)) {
                continue;
            }
            let response = response(result.map(Option::unwrap));
            // A client disappearing must not take down the single writer.
            let _ = writeln!(stream, "{response}");
            if shutdown {
                stopping = true;
            }
        }
    }
    fn handle(&mut self, request: Request) -> Result<Value> {
        Ok(match request {
            Request::NotebookTransport {
                binding,
                generation,
                owner,
                event,
            } => {
                binding.validate(&mut self.store)?;
                if generation != self.store.generation() {
                    return Err(conflict(
                        "notebook console generation changed; execution is lost",
                    ));
                }
                let (instance, session) = owner
                    .split_once(':')
                    .ok_or_else(|| invalid("invalid notebook owner"))?;
                if session.len() != 64 || !session.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return Err(invalid("invalid notebook owner"));
                }
                self.consoles.owns(&binding, instance)?;
                self.notebooks
                    .transport(&self.store, &binding, &owner, event)?
            }
            Request::NotebookHeartbeat {
                binding,
                generation,
                instance,
                sessions,
            } => {
                binding.validate(&mut self.store)?;
                if generation != self.store.generation() {
                    return Err(conflict("notebook console generation changed"));
                }
                self.consoles.owns(&binding, &instance)?;
                self.notebooks.heartbeat(&instance, &sessions)?;
                json!({"accepted":true})
            }
            Request::ConsoleAction {
                binding,
                generation,
                owner,
                action,
            } => {
                binding.validate(&mut self.store)?;
                if generation != self.store.generation() {
                    return Err(conflict(
                        "console belongs to a prior daemon generation; query outcomes may be unknown",
                    ));
                }
                let valid_owner = owner.split_once(':').is_some_and(|(instance, session)| {
                    instance
                        .strip_prefix("console-")
                        .is_some_and(|id| id.parse::<OperationId>().is_ok())
                        && session.len() == 64
                        && session.bytes().all(|b| b.is_ascii_hexdigit())
                });
                if !valid_owner {
                    return Err(invalid("invalid console session identity"));
                }
                self.workspace(binding, owner, action)?
            }
            Request::ConsoleOpen { binding, assets } => {
                self.consoles.open(&mut self.store, binding, assets)?
            }
            Request::ConsoleOverview {
                binding,
                generation,
            } => {
                binding.validate(&mut self.store)?;
                if generation != self.store.generation() {
                    return Err(conflict("console belongs to a prior daemon generation"));
                }
                let config = ProjectConfig::read(&binding.worktree)?;
                let runtime = self
                    .cell
                    .as_ref()
                    .map(|c| c.status(&self.store))
                    .transpose()?;
                let branches: Vec<_> = self.store.list_branches(binding.project_id, false)?.into_iter().map(|b| {
                    json!({"id":b.branch.id,"name":b.branch.name,"parent_id":b.branch.parent_id,
                        "desired_state":b.endpoint.desired_state,"revision":b.revision,"observed_revision":b.observed_revision,"is_default":b.is_default,
                        "expired":b.expired})
                }).collect();
                json!({"api_version":crate::console::assets::VERSION,"project":{"id":config.id,"name":config.name},
                    "worktree":binding.worktree,"data_dir":self.store.root(),"branches":branches,
                    "runtime":{"ready":runtime.as_ref().is_some_and(|r|r["ready"]==true),
                        "engine_enabled":self.cell.is_some(),"generation":generation,"postgres_major":17,
                        "needs_attention":runtime.as_ref().is_some_and(|r|!r["last_error"].is_null())},
                    "capabilities":{"overview":true,"sql":true,"workspace":true,"ingestion":true,"notebooks":true,"notebook_runtime":1,"notebook_environments":1,"notebook_packages":1,"notebook_environment_controls":1,"notebook_environment_adoption":true},
                    "limits":{"active_branches":32}})
            }
            Request::Api { .. } => {
                return Err(invalid(
                    "application request must use the versioned dispatcher",
                ));
            }
            Request::Status => {
                json!({"environment_error":self.environments.last_error,"notebook_events":self.notebooks.events,"notebook_error":self.notebooks.last_error,"ingest_error":self.ingest_error,"console_error":self.consoles.last_error,"analytical_sessions_error":self.sessions.last_error,"analytical_sessions_active":self.store.active_analytical_sessions()?.len(),"analytics_recovery":self.publisher.recovery,"analytics_error":self.publisher.last_error,"sql_workers_active":self.queries.len()+self.console_queries.active(),"generation":self.store.generation(),"schema_version":SCHEMA_VERSION,"pending_operations":self.store.pending()?.len(),"engine_execution":self.cell.is_some(),"runtime":self.cell.as_ref().map(|c|c.status(&self.store)).transpose()?,"gateway":self.gateway.as_ref().map(|g|g.status())})
            }
            Request::RegisterProject { config } => {
                self.store.register_project(&config)?;
                json!(config)
            }
            Request::Submit {
                project_id,
                key,
                mutation,
            } => {
                if let Some(cell) = &self.cell {
                    cell.validate_mutation(&mutation)?;
                }
                serde_json::to_value(self.store.submit(project_id, &key, mutation)?)?
            }
            Request::Operation { id } => serde_json::to_value(self.store.operation(id)?)?,
            Request::Pending => serde_json::to_value(self.store.pending()?)?,
            Request::Branch { id } => serde_json::to_value(self.store.branch(id)?)?,
            Request::RenameBranch {
                project_id,
                id,
                name,
            } => {
                self.store.rename_branch(project_id, id, &name)?;
                json!({"id":id})
            }
            Request::SelectWorktree {
                path,
                project_id,
                branch_id,
            } => {
                self.store.select_worktree(&path, project_id, branch_id)?;
                json!({"branch_id":branch_id})
            }
            Request::Selection { path, project_id } => {
                json!({"branch_id":self.store.selected_branch(&path,project_id)?})
            }
            Request::ListBranches {
                project_id,
                include_deleted,
            } => json!(self.store.list_branches(project_id, include_deleted)?),
            Request::GetBranch { project_id, id } => {
                json!(self.store.branch_in_project(project_id, id)?)
            }
            Request::Connection { project_id, id } => {
                self.store.branch_in_project(project_id, id)?;
                self.store.accepting_work(id)?;
                let cell = self
                    .cell
                    .as_ref()
                    .ok_or_else(|| conflict("engine is disabled"))?;
                self.gateway
                    .as_mut()
                    .ok_or_else(|| conflict("connection gateway unavailable"))?
                    .ensure_listener(&mut self.store, cell, id)?;
                self.store.stable_connection_json(project_id, id)?
            }
            Request::AcquireLease {
                project_id,
                branch_id,
                holder,
                ttl_ms,
            } => {
                self.store.branch_in_project(project_id, branch_id)?;
                json!(self.store.acquire_lease(
                    branch_id,
                    None,
                    &holder,
                    Duration::from_millis(ttl_ms)
                )?)
            }
            Request::ReleaseLease { project_id, lease } => {
                self.store.branch_in_project(project_id, lease.branch_id)?;
                self.store.release_lease(&lease)?;
                json!({"released":true})
            }
            Request::RenewLease {
                project_id,
                lease,
                ttl_ms,
            } => {
                self.store.branch_in_project(project_id, lease.branch_id)?;
                json!(
                    self.store
                        .renew_lease(&lease, Duration::from_millis(ttl_ms))?
                )
            }
            Request::Shutdown => json!({"stopping":true}),
            Request::AuthorizeProcess {
                role,
                generation,
                token,
                pid,
            } => {
                if role.starts_with("notebook-kernel-") {
                    self.notebooks
                        .authorize(&mut self.store, &role, generation, &token, pid)?;
                } else {
                    self.cell
                        .as_ref()
                        .ok_or_else(|| conflict("engine is disabled"))?
                        .authorize(&mut self.store, &role, generation, &token, pid)?;
                }
                json!({"authorized":true})
            }
        })
    }
    fn workspace(
        &mut self,
        binding: crate::api::Binding,
        owner: String,
        action: crate::console::workspace::Command,
    ) -> Result<Value> {
        use crate::console::workspace::{Command as C, identifier};
        let scope = json!([binding.project_id, binding.worktree, owner]).to_string();
        let (id, target, query) = match action {
            C::NotebookRefresh { target, key } => {
                target.validate(&self.store, &binding)?;
                crate::notebooks::contract::key(&key)?;
                use sha2::Digest;
                let key = hex::encode(sha2::Sha256::digest(
                    json!([scope, key]).to_string().as_bytes(),
                ));
                return crate::sessions::Sessions::refresh(
                    &mut self.store,
                    self.cell.as_ref(),
                    &binding,
                    target.branch.to_string(),
                    format!("console:{key}"),
                    Default::default(),
                );
            }
            C::NotebookRefreshStatus { id } => {
                return self.store.refresh_status(binding.project_id, id);
            }
            C::NotebookCancelRefresh { id } => {
                return crate::sessions::Sessions::cancel_refresh(
                    &mut self.store,
                    binding.project_id,
                    id,
                );
            }
            C::Notebook { command } => {
                let instance = owner
                    .split_once(':')
                    .ok_or_else(|| invalid("invalid notebook owner"))?
                    .0;
                self.consoles.owns(&binding, instance)?;
                return self.notebooks.handle(
                    &mut self.store,
                    self.cell.as_ref(),
                    &binding,
                    &owner,
                    command,
                );
            }
            C::Environment { command } => {
                self.consoles.owns(
                    &binding,
                    owner
                        .split_once(':')
                        .ok_or_else(|| invalid("invalid console owner"))?
                        .0,
                )?;
                return self.environments.handle(&mut self.store, &binding, command);
            }
            C::NotebookFiles { command } => {
                self.consoles.owns(
                    &binding,
                    owner
                        .split_once(':')
                        .ok_or_else(|| invalid("invalid notebook owner"))?
                        .0,
                )?;
                return crate::notebooks::files::handle(&binding.worktree, command);
            }
            C::Ingest { command } => {
                return self.uploads.handle(
                    &mut self.store,
                    &mut self.ingestion,
                    &binding,
                    &owner,
                    command,
                );
            }
            C::Query {
                id,
                target,
                sql,
                read_only,
                max_rows,
                timeout_ms,
            } => (
                id,
                target,
                crate::query::Query {
                    sql,
                    read_only,
                    max_rows,
                    timeout_ms,
                },
            ),
            C::Catalog { id, target } => (id, target, crate::query::Query::catalog()),
            C::Preview {
                id,
                target,
                schema,
                table,
            } => (
                id,
                target,
                crate::query::Query {
                    sql: format!(
                        "SELECT * FROM {}.{} LIMIT 200",
                        identifier(&schema)?,
                        identifier(&table)?
                    ),
                    read_only: true,
                    max_rows: 200,
                    timeout_ms: 10000,
                },
            ),
            C::QueryStatus { id } => return self.console_queries.status(&scope, id, false),
            C::CancelQuery { id } => return self.console_queries.status(&scope, id, true),
            C::Connect { target } => {
                target.validate(&self.store, &binding)?;
                return self.handle(Request::Connection {
                    project_id: binding.project_id,
                    id: target.branch,
                });
            }
            command @ (C::SavedList
            | C::SavedGet { .. }
            | C::SavedPut { .. }
            | C::SavedDelete { .. }) => {
                return crate::console::workspace::saved(&self.store, &binding, command);
            }
            other => {
                let action = other.mutation(&self.store, &binding)?;
                return self
                    .public_action(binding, action)
                    .map(crate::console::workspace::operation);
            }
        };
        query.validate()?;
        if let Some(value) = self.console_queries.existing(&scope, id, &target, &query)? {
            return Ok(value);
        }
        target.validate(&self.store, &binding)?;
        if self.queries.len() + self.console_queries.active() >= crate::query::WORKERS {
            return Err(conflict("all four SQL workers are busy"));
        }
        let connection = self.handle(Request::Connection {
            project_id: binding.project_id,
            id: target.branch,
        })?;
        self.console_queries.start(
            scope,
            id,
            target,
            query,
            connection,
            self.store.generation(),
        )
    }
    fn public_action(
        &mut self,
        binding: crate::api::Binding,
        action: crate::api::Action,
    ) -> Result<Value> {
        if let crate::api::Action::Environment { command } = action {
            return self.environments.handle(&mut self.store, &binding, command);
        }
        if let crate::api::Action::IngestInspect {
            path,
            delimiter,
            header,
            null_strings,
        } = action
        {
            return self.ingestion.inspect(
                &mut self.store,
                &binding,
                path,
                delimiter,
                header,
                null_strings,
            );
        }
        if let crate::api::Action::Connect { branch } = action {
            let id = crate::api::resolve(&self.store, &binding, branch.as_deref())?;
            return self.handle(Request::Connection {
                project_id: binding.project_id,
                id,
            });
        }
        crate::api::handle(&mut self.store, self.cell.as_ref(), &binding, action)
    }
}
fn response(result: Result<Value>) -> Value {
    match result {
        Ok(value) => json!({"version":1,"result":value}),
        Err(Error::Operation(error)) => json!({"version":1,"error":error}),
        Err(error) => {
            // Keep request contents, SQL, credentials and filesystem paths out
            // of diagnostics, but preserve OS/SQLite codes for an unavailable
            // response. Previously daemon.log stayed empty for these failures.
            let cause = match &error {
                Error::Io(error) => {
                    json!({"kind":"io","os_code":error.raw_os_error(),"category":format!("{:?}", error.kind())})
                }
                Error::Sql(rusqlite::Error::SqliteFailure(code, _)) => {
                    json!({"kind":"sqlite","code":format!("{:?}",code.code),"extended_code":code.extended_code})
                }
                Error::Sql(_) => json!({"kind":"sqlite_conversion"}),
                Error::Json(error) => {
                    json!({"kind":"json","category":format!("{:?}",error.classify())})
                }
                _ => json!({"kind":"local_configuration"}),
            };
            eprintln!("{}", json!({"event":"local_request_failed","cause":cause}));
            json!({"version":1,"error":{"code":"unavailable","detail":"local request failed; check project path and daemon diagnostics"}})
        }
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        // Close client sockets before releasing installation ownership.
        self.gateway.take();
        // Revoke generation validation before releasing installation ownership.
        self.validator.take();
        // Drop runs while Store still holds the installation lock.
        let _ = fs::remove_file(&self.socket);
    }
}
fn read_request(stream: &mut UnixStream) -> Result<Envelope> {
    let mut bytes = Vec::new();
    BufReader::new(stream)
        .take(crate::notebooks::files::MAX_DOCUMENT_BYTES as u64 + 8193)
        .read_until(b'\n', &mut bytes)?;
    if bytes.len() > crate::notebooks::files::MAX_DOCUMENT_BYTES + 8192
        || bytes.last() != Some(&b'\n')
    {
        return Err(invalid(
            "request must be a newline-terminated JSON object within the transport size limit",
        ));
    }
    let envelope: Envelope =
        serde_json::from_slice(&bytes).map_err(|_| invalid("invalid local API request"))?;
    if bytes.len() as u64 > LIMIT
        && !matches!(
            &envelope.request,
            Request::ConsoleAction {
                action: crate::console::workspace::Command::NotebookFiles { .. },
                ..
            }
        )
    {
        return Err(invalid("request exceeds 64 KiB"));
    }
    Ok(envelope)
}
