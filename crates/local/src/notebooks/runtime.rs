use super::*;
use crate::supervisor::OwnedProcess;
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    net::TcpListener,
    os::unix::fs::{DirBuilderExt, MetadataExt},
    os::unix::process::ExitStatusExt,
    path::Path,
};

pub(super) fn worker(store: &Store) -> Result<(PathBuf, PathBuf)> {
    if crate::installation::Installation::discover()?
        .is_some_and(|i| i.manifest.provenance.get("notebooks").is_none())
    {
        return Err(conflict(
            "notebook runtime is not included in this installation",
        ));
    }
    let (python, exporter) = crate::installation::analytical_worker(store.root())?;
    let worker = exporter
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| invalid("invalid analytical installation"))?
        .join("notebooks/server.py");
    if !worker.is_file() {
        return Err(conflict(
            "notebook runtime is not included in this installation",
        ));
    }
    Ok((python, worker))
}
fn directory(path: &Path) -> Result<()> {
    if !path.exists() {
        fs::DirBuilder::new().mode(0o700).create(path)?;
    }
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } || m.mode() & 0o077 != 0 {
        return Err(conflict(
            "notebook runtime directory must be private and owned",
        ));
    }
    Ok(())
}
fn read(path: &Path) -> Result<Value> {
    let mut bytes = Vec::new();
    fs::File::open(path)?.take(65537).read_to_end(&mut bytes)?;
    if bytes.len() > 65536 {
        return Err(conflict("notebook runtime report exceeds its bound"));
    }
    Ok(serde_json::from_slice(&bytes)?)
}
fn record(store: &Store, role: &str) -> Result<Option<OwnedProcess>> {
    Ok(store
        .native_processes()?
        .into_iter()
        .find(|p| p.role == role))
}
fn alive(store: &Store, role: &str) -> Result<bool> {
    let Some(p) = record(store, role)? else {
        return Ok(false);
    };
    let Some(identity) = supervisor::os::identity(p.pid)? else {
        return Ok(false);
    };
    if identity.start != p.start_identity {
        return Err(conflict("notebook PID identity changed"));
    }
    Ok(!identity.zombie && !supervisor::members(&p)?.is_empty())
}
fn stop_role(store: &mut Store, role: &str) -> Result<()> {
    if let Some(p) = record(store, role)? {
        supervisor::stop(&p)?;
        store.forget_native_process(&p)?;
    }
    Ok(())
}
fn rss(store: &Store, role: &str) -> Result<u64> {
    let Some(p) = record(store, role)? else {
        return Ok(0);
    };
    supervisor::members(&p)?
        .into_iter()
        .try_fold(0u64, |total, pid| {
            Ok(total.saturating_add(supervisor::os::rss(pid)?))
        })
}
impl Notebooks {
    fn ensure_server(&mut self, store: &mut Store, binding: &Binding) -> Result<()> {
        if self.servers.contains_key(&binding.worktree) {
            return Ok(());
        }
        if self.servers.len() >= 2 {
            return Err(conflict("both notebook server slots are occupied"));
        }
        let (python, worker) = worker(store)?;
        let root = store.root().join("notebook-work");
        directory(&root)?;
        let role = format!("notebook-server-{}", OperationId::new());
        let dir = root.join(&role);
        directory(&dir)?;
        for name in [
            "runtime",
            "config",
            "data",
            "ipython",
            "commands",
            "kernels",
            "reports",
            "empty-contents",
        ] {
            directory(&dir.join(name))?;
        }
        let token = crate::console::secret()?;
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr()?.port();
        drop(listener);
        let config = dir.join("server.json");
        supervisor::write_json(
            &config,
            &json!({"workspace":dir,"port":port,"token":token,"project":binding.worktree,"frame_bytes":contract::FRAME_BYTES,"cell_output_bytes":contract::CELL_OUTPUT_BYTES}),
        )?;
        let launch = Launch {
            root: store.root().into(),
            generation: store.generation(),
            role: role.clone(),
            token: crate::console::secret()?,
            branch: None,
            argv: vec![
                python.to_string_lossy().into(),
                "-E".into(),
                "-s".into(),
                "-B".into(),
                worker.to_string_lossy().into(),
                config.to_string_lossy().into(),
            ],
            env: BTreeMap::from([
                ("PATH".into(), "/usr/bin:/bin".into()),
                ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
                (
                    "JUPYTER_CONFIG_DIR".into(),
                    dir.join("config").to_string_lossy().into(),
                ),
                (
                    "JUPYTER_DATA_DIR".into(),
                    dir.join("data").to_string_lossy().into(),
                ),
                (
                    "JUPYTER_RUNTIME_DIR".into(),
                    dir.join("runtime").to_string_lossy().into(),
                ),
                (
                    "IPYTHONDIR".into(),
                    dir.join("ipython").to_string_lossy().into(),
                ),
                ("JUPYTER_NO_CONFIG".into(), "1".into()),
                ("OTEL_SDK_DISABLED".into(), "true".into()),
            ]),
            cwd: binding.worktree.clone(),
        };
        let child = supervisor::start_owned(
            store,
            &launch,
            &dir.join("launch.json"),
            &dir.join("server.log"),
        )?;
        self.servers.insert(
            binding.worktree.clone(),
            Server {
                role,
                dir,
                token,
                child,
                started: now(),
                idle_since: now(),
                port: None,
            },
        );
        Ok(())
    }
    pub(super) fn start(
        &mut self,
        store: &mut Store,
        cell: Option<&crate::engine::Cell>,
        e: &mut Entry,
    ) -> Result<()> {
        e.target.validate(store, &e.binding)?;
        if store.active_analytical_sessions()?.len() >= 2 {
            return Err(conflict("both analytical session slots are occupied"));
        }
        self.ensure_server(store, &e.binding)?;
        let generation = e
            .generation
            .checked_add(1)
            .ok_or_else(|| conflict("notebook generation overflow"))?;
        let session: crate::store::AnalyticalSession =
            serde_json::from_value(Sessions::open_notebook(
                store,
                cell,
                &e.binding,
                e.target.branch.to_string(),
                format!("notebook:{}:{generation}", e.id),
                e.limits.lifetime_ms,
            )?)?;
        e.generation = generation;
        e.session = Some(session.id);
        e.kernel = Some(OperationId::new());
        e.state = "starting".into();
        e.error = None;
        e.started = now();
        e.expires = session.expires_at_ms;
        e.activity = now();
        e.launch = None;
        e.interrupt_at = None;
        e.stop_reason = None;
        e.restart = false;
        Ok(())
    }
    fn report_path(&self, e: &Entry) -> Result<PathBuf> {
        let server = self
            .servers
            .get(&e.binding.worktree)
            .ok_or_else(|| conflict("notebook server missing"))?;
        Ok(server.dir.join("reports").join(format!(
            "{}.json",
            e.kernel.ok_or_else(|| conflict("kernel is not started"))?
        )))
    }
    pub(super) fn control(&self, e: &Entry, action: &str) -> Result<()> {
        let server = self
            .servers
            .get(&e.binding.worktree)
            .ok_or_else(|| conflict("notebook server missing"))?;
        let command = server
            .dir
            .join("commands")
            .join(format!("{}.json", OperationId::new()));
        supervisor::write_json(
            &command,
            &json!({"action":action,"kernel_id":e.kernel,"generation":e.generation}),
        )
    }
    fn prepare_kernel(&self, store: &mut Store, e: &mut Entry) -> Result<()> {
        let session = store.analytical_session(e.binding.project_id, e.session.unwrap())?;
        if session.state != "ready" {
            return Ok(());
        }
        let server = self
            .servers
            .get(&e.binding.worktree)
            .ok_or_else(|| conflict("notebook server lost"))?;
        if server.port.is_none() {
            return Ok(());
        }
        let (python, worker) = worker(store)?;
        let kernel = e.kernel.unwrap();
        let context = server
            .dir
            .join("kernels")
            .join(format!("{kernel}.context.json"));
        let ready = server
            .dir
            .join("kernels")
            .join(format!("{kernel}.ready.json"));
        supervisor::write_json(
            &context,
            &json!({"endpoint":session.endpoint,"epoch_id":session.epoch_id,"ready":ready,"fault":server.dir.join("kernels").join(format!("{kernel}.fault.json"))}),
        )?;
        let launch = Launch {
            root: store.root().into(),
            generation: store.generation(),
            role: format!("notebook-kernel-{}", session.id),
            token: crate::console::secret()?,
            branch: None,
            argv: vec![
                python.to_string_lossy().into(),
                "-E".into(),
                "-s".into(),
                "-B".into(),
                worker.with_file_name("kernel.py").to_string_lossy().into(),
                "-f".into(),
                server
                    .dir
                    .join("runtime")
                    .join(format!("kernel-{kernel}.json"))
                    .to_string_lossy()
                    .into(),
                format!(
                    "--IPKernelApp.exec_files=[{}]",
                    serde_json::to_string(
                        &worker.with_file_name("bootstrap.py").to_string_lossy()
                    )?
                ),
            ],
            env: BTreeMap::from([
                ("PATH".into(), "/usr/bin:/bin".into()),
                (
                    "SUPABRICKS_NOTEBOOK_CONTEXT".into(),
                    context.to_string_lossy().into(),
                ),
                ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
                (
                    "IPYTHONDIR".into(),
                    server.dir.join("ipython").to_string_lossy().into(),
                ),
                ("OTEL_SDK_DISABLED".into(), "true".into()),
            ]),
            cwd: e.binding.worktree.clone(),
        };
        let path = server
            .dir
            .join("kernels")
            .join(format!("{kernel}.launch.json"));
        supervisor::write_json(&path, &launch)?;
        supervisor::write_json(
            &server
                .dir
                .join("kernels")
                .join(format!("{kernel}.gate.json")),
            &json!({"binary":std::env::current_exe()?,"launch":path,"ready":ready,"token":launch.token}),
        )?;
        e.launch = Some(launch);
        self.control(e, "start")
    }
    pub fn authorize(
        &mut self,
        store: &mut Store,
        role: &str,
        generation: i64,
        token: &str,
        pid: u32,
    ) -> Result<()> {
        if generation != store.generation() {
            return Err(conflict("notebook belongs to a stale daemon generation"));
        }
        let e = self
            .entries
            .values()
            .find(|e| e.launch.as_ref().is_some_and(|l| l.role == role))
            .ok_or_else(|| conflict("notebook launch is not pending"))?;
        let launch = e.launch.as_ref().unwrap();
        if launch.token != token
            || e.state != "starting"
            || now() >= e.expires
            || self.owners.get(&e.owner).is_none_or(|t| *t <= now())
        {
            return Err(conflict("notebook launch was revoked"));
        }
        if store
            .analytical_session(e.binding.project_id, e.session.unwrap())?
            .state
            != "ready"
        {
            return Err(conflict(
                "notebook requires its admitted analytical session",
            ));
        }
        if !alive(
            store,
            &self
                .servers
                .get(&e.binding.worktree)
                .ok_or_else(|| conflict("notebook server lost"))?
                .role,
        )? {
            return Err(conflict("notebook server lost"));
        }
        let evidence = supervisor::evidence(launch, pid)?;
        store.record_native_process(&evidence)
    }
    fn stop_entry(&self, store: &mut Store, sessions: &mut Sessions, e: &mut Entry) -> Result<()> {
        // Sessions::finish independently enforces this ordering for expiry,
        // branch deletion and CLI closure of a notebook's analytical session.
        if let Some(id) = e.session {
            stop_role(store, &format!("notebook-kernel-{id}"))?;
            let mut session = store.analytical_session(e.binding.project_id, id)?;
            if !matches!(session.state.as_str(), "closed" | "failed") {
                sessions.finish(store, &mut session, "closed")?;
            }
        }
        if e.kernel.is_some() && self.servers.contains_key(&e.binding.worktree) {
            self.control(e, "forget")?;
        }
        e.launch = None;
        e.interrupt_at = None;
        let reason = e.stop_reason.as_deref().unwrap_or("stopped");
        e.state = match reason {
            "stopped" | "restart" | "session_revoked" => "stopped",
            "expired" | "idle_expired" => "expired",
            "bootstrap_failed" | "output_limit" | "memory_limit" => "failed",
            _ => "lost",
        }
        .into();
        e.error = if matches!(reason, "stopped" | "restart") {
            None
        } else {
            Some(reason.into())
        };
        Ok(())
    }
    pub fn tick(
        &mut self,
        store: &mut Store,
        sessions: &mut Sessions,
        cell: Option<&crate::engine::Cell>,
        stopping: bool,
    ) -> Result<()> {
        self.owners.retain(|_, t| *t > now());
        let mut failed_servers = Vec::new();
        for (path, server) in &mut self.servers {
            let exited = server.child.try_wait()?;
            let resident = if exited.is_some() {
                0
            } else {
                rss(store, &server.role)?
            };
            if exited.is_some()
                || !alive(store, &server.role)?
                || resident > contract::SERVER_RSS_BYTES
                || (server.port.is_none() && now() - server.started > 30_000)
            {
                self.events.push(json!({"role":server.role,"exit_code":exited.and_then(|s|s.code()),"signal":exited.and_then(|s|s.signal()),"rss_bytes":resident,"rss_limit_bytes":contract::SERVER_RSS_BYTES}));
                if self.events.len() > 16 {
                    self.events.remove(0);
                }
                // Keep one bounded private failure log until down/recovery;
                // never copy its text into API status or release artifacts.
                if let Ok(mut log) = fs::File::open(server.dir.join("server.log")) {
                    let length = log.metadata()?.len();
                    log.seek(SeekFrom::Start(length.saturating_sub(65536)))?;
                    let mut bytes = Vec::new();
                    log.take(65536).read_to_end(&mut bytes)?;
                    supervisor::write_private(
                        &store.root().join("notebook-work/last-server-failure.log"),
                        &bytes,
                    )?;
                }
                failed_servers.push(path.clone());
                continue;
            }
            if server.port.is_none() && server.dir.join("ready.json").exists() {
                let ready = read(&server.dir.join("ready.json"))?;
                if ready["pid"] != server.child.id() {
                    return Err(conflict("Jupyter readiness identity mismatch"));
                }
                server.port = Some(
                    ready["port"]
                        .as_u64()
                        .filter(|p| *p > 0 && *p <= 65535)
                        .ok_or_else(|| conflict("invalid Jupyter port"))?
                        as u16,
                );
            }
        }
        // Fence servers before kernel cleanup during daemon down or server loss.
        if stopping {
            failed_servers = self.servers.keys().cloned().collect();
        }
        for path in &failed_servers {
            stop_role(store, &self.servers[path].role)?;
        }
        let ids: Vec<_> = self.entries.keys().copied().collect();
        for id in ids {
            let mut e = self.entries.remove(&id).unwrap();
            let result = (|| {
                if !active(&e.state) {
                    return Ok(());
                }
                let reason = if stopping {
                    Some("stopped")
                } else if failed_servers.contains(&e.binding.worktree) {
                    Some("server_lost")
                } else if !self.owners.contains_key(&e.owner) {
                    Some("session_revoked")
                } else if now() >= e.expires {
                    Some("expired")
                } else if e.binding.validate(store).is_err() {
                    Some("project_changed")
                } else {
                    None
                };
                if let Some(reason) = reason {
                    e.state = "stopping".into();
                    e.restart = false;
                    e.stop_reason = Some(reason.into());
                }
                if e.state != "stopping" {
                    let s = store.analytical_session(e.binding.project_id, e.session.unwrap())?;
                    if matches!(s.state.as_str(), "closing" | "closed" | "failed") {
                        e.stop_reason = Some("spark_context_lost".into());
                        e.state = "stopping".into();
                    }
                }
                if e.state == "starting" && e.launch.is_none() {
                    self.prepare_kernel(store, &mut e)?;
                }
                if let Some(launch) = &e.launch {
                    if let Some(p) = record(store, &launch.role)? {
                        if !alive(store, &p.role)? {
                            let fault = self.servers[&e.binding.worktree]
                                .dir
                                .join("kernels")
                                .join(format!("{}.fault.json", e.kernel.unwrap()));
                            let reason = if fault.exists() {
                                read(&fault)?["error"]
                                    .as_str()
                                    .filter(|v| matches!(*v, "output_limit" | "bootstrap_failed"))
                                    .unwrap_or("kernel_lost")
                                    .to_owned()
                            } else {
                                "kernel_lost".into()
                            };
                            e.stop_reason = Some(reason);
                            e.state = "stopping".into();
                        } else if rss(store, &p.role)? > e.limits.kernel_rss_bytes {
                            e.stop_reason = Some("memory_limit".into());
                            e.state = "stopping".into();
                        }
                    } else if e.state != "starting" && e.state != "stopping" {
                        e.stop_reason = Some("kernel_lost".into());
                        e.state = "stopping".into();
                    }
                    let path = self.report_path(&e)?;
                    if path.exists() && e.state != "stopping" {
                        let report = read(&path)?;
                        if report["generation"] != e.generation {
                            return Err(conflict("stale notebook report"));
                        }
                        if let Some(error) = report["error"].as_str() {
                            e.stop_reason = Some(error.into());
                            e.state = "stopping".into();
                        } else if report["ready"] == true {
                            e.activity = report["activity_ms"].as_i64().unwrap_or(e.activity);
                            if let Some(sent) = e.interrupt_at {
                                if report["interrupt_ack_ms"]
                                    .as_i64()
                                    .is_some_and(|t| t >= sent)
                                    && report["state"] == "idle"
                                {
                                    e.interrupt_at = None;
                                    e.state = "ready".into();
                                } else if now() - sent > 5000 {
                                    e.stop_reason = Some("spark_interrupt_escalated".into());
                                    e.state = "stopping".into();
                                }
                            } else {
                                e.state = if report["state"] == "busy" {
                                    "busy"
                                } else {
                                    "ready"
                                }
                                .into();
                            }
                            if now() - e.activity.max(e.connected) > e.limits.idle_ms as i64
                                && e.state == "ready"
                            {
                                e.stop_reason = Some("idle_expired".into());
                                e.state = "stopping".into();
                            }
                        }
                    }
                }
                if e.state == "starting" && now() - e.started > 120_000 {
                    e.stop_reason = Some("bootstrap_failed".into());
                    e.state = "stopping".into();
                }
                if e.state == "stopping" {
                    self.stop_entry(store, sessions, &mut e)?;
                    if e.restart {
                        self.start(store, cell, &mut e)?;
                    }
                }
                Ok(())
            })();
            // Runtime errors fence the handle; cleanup is retried on the next tick.
            if let Err(error) = &result {
                self.events
                    .push(json!({"phase":"kernel_tick","handle":id,"error":error.to_string()}));
                if self.events.len() > 16 {
                    self.events.remove(0);
                }
                e.restart = false;
                e.state = "stopping".into();
                e.stop_reason = Some("runtime_failure".into());
                e.error = Some(error.to_string());
            }
            self.entries.insert(id, e);
            result?;
        }
        for (path, server) in &mut self.servers {
            if self
                .entries
                .values()
                .any(|e| e.binding.worktree == *path && active(&e.state))
            {
                server.idle_since = now();
            } else if now() - server.idle_since > 60_000 && !failed_servers.contains(path) {
                failed_servers.push(path.clone());
            }
        }
        for path in failed_servers {
            let server = &self.servers[&path];
            stop_role(store, &server.role)?;
            let mut server = self.servers.remove(&path).unwrap();
            let _ = server.child.wait();
            fs::remove_dir_all(&server.dir)?;
        }
        self.entries
            .retain(|_, e| active(&e.state) || self.owners.contains_key(&e.owner));
        Ok(())
    }
}
