//! Project-bound browser bridge. The daemon owns children; HTTP never opens Store.
pub mod assets;
mod server;
pub mod workspace;
use crate::{
    api::Binding,
    client,
    daemon::Request,
    installation::Installation,
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
    supervisor::{self, Launch},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_core::resource::OperationId;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    root: PathBuf,
    workspace: PathBuf,
    assets: PathBuf,
    binding: Binding,
    generation: i64,
    instance: String,
}
struct Entry {
    config: Config,
    child: Child,
    started: Instant,
}
#[derive(Default)]
pub struct Consoles {
    entries: BTreeMap<PathBuf, Entry>,
    pub last_error: Option<String>,
}
pub(crate) fn secret() -> Result<String> {
    let mut bytes = [0; 32];
    fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(hex::encode(bytes))
}
fn directory(path: &Path) -> Result<()> {
    if !path.try_exists()? {
        fs::DirBuilder::new().mode(0o700).create(path)?;
    }
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } || m.mode() & 0o077 != 0 {
        return Err(conflict(
            "console workspace must be a private owned directory",
        ));
    }
    Ok(())
}
impl Consoles {
    pub fn recover(store: &mut Store) -> Result<Self> {
        let mut this = Self::default();
        this.stop(store)?;
        Ok(this)
    }
    pub fn stop(&mut self, store: &mut Store) -> Result<()> {
        for record in store
            .native_processes()?
            .into_iter()
            .filter(|p| p.role.starts_with("console-"))
        {
            if record.root != store.root() {
                return Err(conflict("console belongs to a different root"));
            }
            supervisor::stop(&record)?;
            store.forget_native_process(&record)?;
            let workspace = store.root().join("tmp").join(&record.role);
            if record
                .role
                .strip_prefix("console-")
                .is_some_and(|id| id.parse::<OperationId>().is_ok())
                && workspace.is_dir()
            {
                fs::remove_dir_all(workspace)?;
            }
        }
        for entry in self.entries.values_mut() {
            let _ = entry.child.wait();
        }
        self.entries.clear();
        Ok(())
    }
    pub fn tick(&mut self, store: &mut Store) -> Result<()> {
        let mut gone = Vec::new();
        for (path, entry) in &mut self.entries {
            if entry.child.try_wait()?.is_some() {
                gone.push(path.clone());
            }
        }
        for path in gone {
            let entry = &self.entries[&path];
            if let Some(record) = store
                .native_processes()?
                .into_iter()
                .find(|p| p.role == entry.config.instance)
            {
                supervisor::stop(&record)?;
                store.forget_native_process(&record)?;
            }
            fs::remove_dir_all(&entry.config.workspace)?;
            self.entries.remove(&path);
        }
        Ok(())
    }
    pub fn open(
        &mut self,
        store: &mut Store,
        binding: Binding,
        asset_path: PathBuf,
    ) -> Result<Value> {
        binding.validate(store)?;
        // An installed daemon cannot be made to serve an alternate frontend.
        let asset_path = if let Some(i) = Installation::discover()? {
            let expected = i.root.join("share/console");
            if asset_path.canonicalize()? != expected.canonicalize()? {
                return Err(invalid("installed console assets cannot be overridden"));
            }
            expected
        } else {
            asset_path.canonicalize()?
        };
        let _ = assets::Assets::load(&asset_path)?;
        self.tick(store)?;
        if let Some(entry) = self.entries.get(&binding.worktree)
            && entry.config.binding.project_id != binding.project_id
        {
            return Err(conflict(
                "project identity changed; restart the console with supabricks down then console",
            ));
        }
        if !self.entries.contains_key(&binding.worktree) {
            if self.entries.len() >= 4 {
                return Err(conflict(
                    "four project consoles are already open; stop the cell to close them",
                ));
            }
            directory(&store.root().join("tmp"))?;
            let instance = format!("console-{}", OperationId::new());
            let workspace = store.root().join("tmp").join(&instance);
            directory(&workspace)?;
            let config = Config {
                root: store.root().to_owned(),
                workspace: workspace.clone(),
                assets: asset_path,
                binding: binding.clone(),
                generation: store.generation(),
                instance: instance.clone(),
            };
            let config_path = workspace.join("config.json");
            supervisor::write_json(&config_path, &config)?;
            let launch = Launch {
                root: store.root().to_owned(),
                generation: store.generation(),
                role: instance,
                token: secret()?,
                branch: None,
                argv: vec![
                    std::env::current_exe()?.to_string_lossy().into_owned(),
                    "console-serve".into(),
                    "--config".into(),
                    config_path.to_string_lossy().into_owned(),
                    "--data-dir".into(),
                    store.root().to_string_lossy().into_owned(),
                ],
                env: BTreeMap::from([("PATH".into(), "/usr/bin:/bin".into())]),
                cwd: store.root().to_owned(),
            };
            let child = supervisor::start_owned(
                store,
                &launch,
                &workspace.join("launch.json"),
                &workspace.join("server.log"),
            )?;
            self.entries.insert(
                binding.worktree.clone(),
                Entry {
                    config,
                    child,
                    started: Instant::now(),
                },
            );
        }
        let entry = &self.entries[&binding.worktree];
        let ready = entry.config.workspace.join("ready.json");
        if !ready.exists() {
            if entry.started.elapsed() > Duration::from_secs(10) {
                return Err(conflict(
                    "console startup timed out; inspect its private server.log under data/tmp, then restart the cell",
                ));
            }
            return Ok(json!({"state":"starting"}));
        }
        let ready: Value = serde_json::from_slice(&fs::read(ready)?)?;
        let port = ready["port"]
            .as_u64()
            .filter(|p| *p > 0 && *p <= 65535)
            .ok_or_else(|| invalid("invalid console port"))?;
        if ready["instance"] != entry.config.instance || ready["pid"] != entry.child.id() {
            return Err(conflict(
                "console readiness identity differs from owned child",
            ));
        }
        let token = secret()?;
        let mut tickets = 0;
        for file in fs::read_dir(&entry.config.workspace)? {
            let file = file?;
            if file.file_name().to_string_lossy().starts_with("ticket-") {
                let bytes = match fs::read(file.path()) {
                    Ok(bytes) => bytes,
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(e) => return Err(e.into()),
                };
                let value: Value = serde_json::from_slice(&bytes)?;
                if value["expires_at_ms"]
                    .as_i64()
                    .is_none_or(|t| t <= chrono::Utc::now().timestamp_millis())
                {
                    match fs::remove_file(file.path()) {
                        Ok(()) => {}
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                        Err(e) => return Err(e.into()),
                    }
                } else {
                    tickets += 1;
                }
            }
        }
        if tickets >= 32 {
            return Err(conflict(
                "too many unused console links; use an existing link or wait 60 seconds",
            ));
        }
        supervisor::write_json(
            &entry.config.workspace.join(format!("ticket-{token}.json")),
            &json!({"token":token,"expires_at_ms":chrono::Utc::now().timestamp_millis()+60_000}),
        )?;
        Ok(
            json!({"state":"ready","url":format!("http://127.0.0.1:{port}/#launch={token}"),
            "project_id":binding.project_id,"worktree":binding.worktree,"api_version":assets::VERSION,
            "launch_expires_in_seconds":60}),
        )
    }
}
impl Drop for Consoles {
    fn drop(&mut self) {
        for entry in self.entries.values_mut() {
            let _ = entry.child.kill();
            let _ = entry.child.wait();
        }
    }
}

pub fn launch(root: PathBuf, project: PathBuf, no_open: bool) -> Result<Value> {
    let c = client::Client::bind(&root, &project)?;
    let asset_path = assets::discover()?;
    let _ = assets::Assets::load(&asset_path)?;
    // Run the existing readiness path without introducing a second JSON result.
    let output = Command::new(std::env::current_exe()?)
        .args(["up", "--data-dir"])
        .arg(&root)
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .status()?;
    if !output.success() {
        return Err(invalid(
            "runtime is not ready; resolve the startup diagnostic above and run supabricks doctor",
        ));
    }
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let mut value = client::request(
            &root,
            Request::ConsoleOpen {
                binding: c.binding.clone(),
                assets: asset_path.clone(),
            },
        )?;
        if value["state"] == "ready" {
            let url = value["url"].as_str().unwrap();
            let opened = if no_open {
                false
            } else {
                Command::new(if cfg!(target_os = "macos") {
                    "/usr/bin/open"
                } else {
                    "xdg-open"
                })
                .arg(url)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map(|mut child| {
                    std::thread::spawn(move || {
                        let _ = child.wait();
                    });
                    true
                })
                .unwrap_or(false)
            };
            value["browser_open_requested"] = json!(opened);
            return Ok(value);
        }
        if Instant::now() >= deadline {
            return Err(conflict(
                "console startup pending; run doctor, then reopen console",
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
pub fn serve(config: &Path) -> Result<()> {
    let config: Config = serde_json::from_slice(&fs::read(config)?)?;
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()?
        .block_on(server::serve(config))
}
