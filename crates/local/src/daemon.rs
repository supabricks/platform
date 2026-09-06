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
    Shutdown,
    AuthorizeProcess {
        role: String,
        generation: i64,
        token: String,
        pid: u32,
    },
}

pub struct Daemon {
    store: Store,
    listener: UnixListener,
    socket: PathBuf,
    cell: Option<crate::engine::Cell>,
}
impl Daemon {
    pub fn bind(root: &Path) -> Result<Self> {
        // Acquire ownership before touching a stale socket or migrating state.
        let mut store = Store::open(root)?;
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
        Ok(Self {
            store,
            listener,
            socket,
            cell,
        })
    }
    pub fn enable_engine(mut self, bundle: &Path, helpers: &Path) -> Result<Self> {
        if self.cell.is_none() {
            crate::engine::RuntimeConfig::initialize(self.store.root(), bundle, helpers)?;
            self.cell = Some(crate::engine::Cell::open(&mut self.store)?);
        }
        Ok(self)
    }
    pub fn serve(mut self) -> Result<()> {
        self.listener.set_nonblocking(true)?;
        let mut next_tick = std::time::Instant::now();
        let mut stopping = false;
        loop {
            if std::time::Instant::now() >= next_tick {
                if let Some(cell) = &mut self.cell {
                    if stopping {
                        if cell.stop(&mut self.store)? {
                            return Ok(());
                        }
                    } else {
                        match cell.tick(&mut self.store) {
                            Ok(()) => cell.last_error = None,
                            Err(e) => cell.last_error = Some(e.to_string()),
                        }
                    }
                } else if stopping {
                    return Ok(());
                }
                next_tick = std::time::Instant::now() + Duration::from_millis(200);
            }
            let (mut stream, _) = match self.listener.accept() {
                Ok(pair) => pair,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(20));
                    continue;
                }
                Err(e) => return Err(e.into()),
            };
            // A probe may close immediately after connect. Socket setup errors
            // belong to that client, not to the daemon's ownership lifetime.
            if stream
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
                self.handle(envelope.request)
            });
            let response = match result {
                Ok(value) => json!({"version":1,"result":value}),
                Err(Error::Operation(error)) => json!({"version":1,"error":error}),
                Err(_) => {
                    json!({"version":1,"error":{"code":"internal","detail":"local state request failed"}})
                }
            };
            // A client disappearing must not take down the single writer.
            let _ = writeln!(stream, "{response}");
            if shutdown {
                stopping = true;
            }
        }
    }
    fn handle(&mut self, request: Request) -> Result<Value> {
        Ok(match request {
            Request::Status => {
                json!({"generation":self.store.generation(),"schema_version":SCHEMA_VERSION,"pending_operations":self.store.pending()?.len(),"engine_execution":self.cell.is_some(),"runtime":self.cell.as_ref().map(|c|c.status(&self.store)).transpose()?})
            }
            Request::RegisterProject { config } => {
                self.store.register_project(&config)?;
                json!(config)
            }
            Request::Submit {
                project_id,
                key,
                mutation,
            } => serde_json::to_value(self.store.submit(project_id, &key, mutation)?)?,
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
            Request::Shutdown => json!({"stopping":true}),
            Request::AuthorizeProcess {
                role,
                generation,
                token,
                pid,
            } => {
                self.cell
                    .as_ref()
                    .ok_or_else(|| conflict("engine is disabled"))?
                    .authorize(&mut self.store, &role, generation, &token, pid)?;
                json!({"authorized":true})
            }
        })
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        // Drop runs while Store still holds the installation lock.
        let _ = fs::remove_file(&self.socket);
    }
}
fn read_request(stream: &mut UnixStream) -> Result<Envelope> {
    let mut bytes = Vec::new();
    BufReader::new(stream)
        .take(LIMIT + 1)
        .read_until(b'\n', &mut bytes)?;
    if bytes.len() as u64 > LIMIT || bytes.last() != Some(&b'\n') {
        return Err(invalid(
            "request must be a newline-terminated JSON object of at most 64 KiB",
        ));
    }
    serde_json::from_slice(&bytes).map_err(|_| invalid("invalid local API request"))
}
