//! Owned, reproducible notebook environments and package transactions.
mod files;
mod selection;
mod transaction;
pub use selection::{DefaultPreparation, Identity, Selected};
#[cfg(test)]
mod tests;
use crate::{
    api::Binding,
    installation::Installation,
    store::{
        Result, Store,
        error::{conflict, invalid, missing},
    },
    supervisor::{self, Launch, OwnedProcess},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Child,
};
use supabricks_core::resource::{OperationId, ProjectId};

const ACTIVE: &[&str] = &["queued", "initializing", "preparing", "verifying"];
fn hash(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
fn base() -> String {
    "base".into()
}
fn diagnostic(error: crate::store::Error) -> String {
    match error {
        crate::store::Error::Operation(error) => error.to_string(),
        crate::store::Error::Io(error) => format!(
            "environment I/O error ({:?}); inspect the private operation log",
            error.kind()
        ),
        _ => "environment verification or catalog error; inspect the private operation log".into(),
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Inputs {
    pub manifest: String,
    pub lock: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Inspect,
    Initialize {
        key: String,
        #[serde(default = "base")]
        template: String,
    },
    Prepare {
        key: String,
        expected: Inputs,
    },
    Manage {
        key: String,
        expected: Inputs,
        change: Change,
        #[serde(default)]
        offline: bool,
    },
    Find {
        key: String,
    },
    Declaration {
        path: PathBuf,
    },
    Status {
        id: OperationId,
    },
    Cancel {
        id: OperationId,
    },
    Collect,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Change {
    Add { requirement: String },
    Remove { package: String },
    Lock,
    Sync,
    Adopt { path: PathBuf, expected: Inputs },
    ExportBundle { path: PathBuf },
    ImportBundle { path: PathBuf },
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Workflow {
    change: Change,
    offline: bool,
}
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Operation {
    pub id: OperationId,
    pub project: ProjectId,
    pub worktree: PathBuf,
    pub key: String,
    pub state: String,
    pub kind: String,
    pub inputs: Inputs,
    pub template: String,
    pub contract: String,
    pub generation: Option<OperationId>,
    pub created: i64,
    pub started: Option<i64>,
    pub worker: Option<OwnedProcess>,
    pub error: Option<String>,
    pub cancel: bool,
    #[serde(default)]
    pub workflow: Option<Workflow>,
    #[serde(default)]
    pub result: Option<Value>,
    #[serde(default)]
    pub publication: Option<Inputs>,
}
impl Operation {
    fn view(&self) -> Value {
        json!({"id":self.id,"state":self.state,"kind":self.kind,"inputs":self.inputs,
        "key":self.key,"generation":self.generation,"created_at_ms":self.created,"error":self.error,"cancel_requested":self.cancel,"workflow":self.workflow,"result":self.result,"published_inputs":self.publication})
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Generation {
    pub id: OperationId,
    pub project: ProjectId,
    pub worktree: PathBuf,
    pub worktree_key: String,
    pub path: PathBuf,
    pub directory: (u64, u64),
    pub inputs: Inputs,
    pub contract: String,
    pub interpreter: PathBuf,
    pub installation: String,
    pub state: String,
    pub inventory: Option<String>,
}
#[derive(Clone, Deserialize)]
pub(crate) struct Template {
    pub manifest: String,
    pub lock: String,
    pub requirements: String,
    pub inputs: Inputs,
    pub packages: BTreeMap<String, String>,
}
#[derive(Deserialize)]
pub(crate) struct Package {
    pub version: u32,
    pub target: String,
    pub python_version: String,
    pub files: BTreeMap<String, String>,
    pub templates: BTreeMap<String, Template>,
    #[serde(skip)]
    pub root: PathBuf,
    #[serde(skip)]
    pub identity: String,
    #[serde(skip)]
    pub installation: String,
}
impl Package {
    fn load(store: &Store) -> Result<Self> {
        let installed = Installation::discover()?;
        let (root, installation) = if let Some(i) = installed {
            (i.root, i.identity)
        } else {
            let (_, worker) = crate::installation::analytical_worker(store.root())?;
            let root = worker
                .parent()
                .and_then(Path::parent)
                .and_then(Path::parent)
                .ok_or_else(|| invalid("invalid environment component root"))?
                .to_path_buf();
            (root, "source".into())
        };
        let bytes = files::read(
            &root.join("python/notebooks/kernel-contract.json"),
            1024 * 1024,
        )
        .map_err(|_| conflict("notebook environment component is not installed"))?;
        let mut package: Self = serde_json::from_slice(&bytes)?;
        if package.version != 1
            || package.python_version != "3.12.13"
            || package.templates.len() > 3
            || package.files.len() > 256
            || package.target
                != if cfg!(target_os = "linux") {
                    "linux-x86_64"
                } else {
                    "macos-arm64"
                }
        {
            return Err(conflict("incompatible notebook environment component"));
        }
        for name in package.files.keys() {
            if !Path::new(name)
                .components()
                .all(|p| matches!(p, std::path::Component::Normal(_)))
            {
                return Err(invalid("invalid component path"));
            }
        }
        for template in package.templates.values() {
            for path in [&template.manifest, &template.lock, &template.requirements] {
                if !package.files.contains_key(path) {
                    return Err(conflict(
                        "environment template is outside the qualified inventory",
                    ));
                }
            }
        }
        package.root = root;
        package.identity = hash(&bytes);
        package.installation = installation;
        Ok(package)
    }
    fn bytes(&self, name: &str) -> Result<Vec<u8>> {
        let expected = self
            .files
            .get(name)
            .ok_or_else(|| conflict("file is outside environment component"))?;
        let bytes = files::read(&self.root.join(name), 1024 * 1024)?;
        if hash(&bytes) != *expected {
            return Err(conflict("environment component checksum mismatch"));
        }
        Ok(bytes)
    }
    fn verify_executable(&self, name: &str) -> Result<()> {
        use crate::notebooks::files::directory::Directory;
        use std::ffi::OsStr;
        use std::io::Read;
        let expected = self
            .files
            .get(name)
            .ok_or_else(|| conflict("missing environment executable inventory"))?;
        let relative = Path::new(name);
        let mut parent = Directory::project(&self.root)?;
        for part in relative.parent().unwrap().components() {
            parent = parent.child(part.as_os_str(), false)?;
        }
        let mut file = parent.open(
            relative.file_name().unwrap_or(OsStr::new("")),
            libc::O_RDONLY,
        )?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.len() > 256 * 1024 * 1024 {
            return Err(conflict("invalid environment executable"));
        }
        let mut digest = Sha256::new();
        let mut buffer = [0; 64 * 1024];
        loop {
            let n = file.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            digest.update(&buffer[..n]);
        }
        if hex::encode(digest.finalize()) != *expected {
            return Err(conflict("environment executable checksum mismatch"));
        }
        Ok(())
    }
    fn template(&self, name: &str) -> Result<&Template> {
        self.templates
            .get(name)
            .ok_or_else(|| invalid("only qualified environment templates are supported"))
    }
}
#[derive(Clone)]
pub struct Limits {
    pub queue: usize,
    pub deadline_ms: i64,
    pub rss: u64,
    pub admission_free: u64,
    pub reserve: u64,
    pub generation_bytes: u64,
    pub cache_bytes: u64,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            queue: 8,
            deadline_ms: 60_000,
            rss: 512 * 1024 * 1024,
            admission_free: 2 * 1024 * 1024 * 1024,
            reserve: 512 * 1024 * 1024,
            generation_bytes: 1024 * 1024 * 1024,
            cache_bytes: 2 * 1024 * 1024 * 1024,
        }
    }
}
#[derive(Default)]
pub struct Manager {
    child: Option<(OperationId, Child)>,
    pub last_error: Option<String>,
    limits: Limits,
}
impl Manager {
    pub fn recover(store: &mut Store) -> Result<Self> {
        // Domain records are durable before the execution gate opens. Also stop
        // a recorded gate whose domain callback was interrupted before commit.
        for p in store
            .native_processes()?
            .into_iter()
            .filter(|p| p.role.starts_with("environment-"))
        {
            supervisor::stop(&p)?;
            store.forget_native_process(&p)?;
        }
        store.clear_environment_leases()?;
        for mut o in store.environment_operations()? {
            if ACTIVE.contains(&o.state.as_str()) {
                o.worker = None;
                if o.kind == "initialize"
                    && files::inputs(&o.worktree).ok().as_ref() == Some(&o.inputs)
                {
                    o.state = "ready".into();
                } else {
                    o.state = if o.cancel { "cancelled" } else { "failed" }.into();
                    o.error = Some("interrupted; inspect declarations and env sync with a new request key; previous declarations are retained in notebooks/.environment-<operation-id>".into());
                }
                store.save_environment_operation(&o)?;
            }
        }
        for mut g in store.environment_generations()? {
            if g.state == "building"
                || (g.state == "ready" && files::generation_path(store, &g, false).is_err())
            {
                g.state = "invalid".into();
                store.invalidate_environment(&g)?;
            }
        }
        Ok(Self::default())
    }
    pub fn handle(
        &mut self,
        store: &mut Store,
        binding: &Binding,
        command: Command,
    ) -> Result<Value> {
        binding.validate(store)?;
        let worktree = binding.worktree.canonicalize()?;
        if worktree != binding.worktree {
            return Err(conflict("environment worktree must be canonical"));
        }
        let project = binding.project_id;
        let operations = store.environment_operations()?;
        let scoped = |o: &&Operation| o.project == project && o.worktree == worktree;
        match command {
            Command::Find { key } => Ok(
                json!({"operation": operations.iter().filter(scoped).find(|o| o.key == key).map(Operation::view)}),
            ),
            Command::Declaration { path } => {
                let (manifest, lock) = transaction::declaration(&worktree, &path)?;
                Ok(json!({"inputs":Inputs {manifest:hash(&manifest),lock:hash(&lock)}}))
            }
            Command::Inspect => {
                let declarations = match files::inputs(&worktree) {
                    Ok(v) => Some(v),
                    Err(crate::store::Error::Io(e)) if e.kind() == std::io::ErrorKind::NotFound => {
                        None
                    }
                    Err(e) => return Err(e),
                };
                let active = store.active_environment(project, &worktree)?;
                let generation = store
                    .environment_generations()?
                    .into_iter()
                    .find(|g| Some(g.id) == active);
                let package = Package::load(store)?;
                let ready = generation.as_ref().is_some_and(|g| {
                    g.state == "ready"
                        && g.contract == package.identity
                        && g.installation == package.installation
                        && Some(&g.inputs) == declarations.as_ref()
                        && files::generation_path(store, g, false).is_ok()
                });
                Ok(
                    json!({"version":1,"inputs":declarations,"active_generation":active,"preparation_needed":!ready,
                    "package_mutation":true,"index":"https://pypi.org/simple","supported_sources":"registry wheels only","protected_packages":package.template("base")?.packages,"operations":operations.iter().filter(scoped).rev().take(20).map(Operation::view).collect::<Vec<_>>()}),
                )
            }
            Command::Status { id } | Command::Cancel { id } => {
                let mut o = operations
                    .iter()
                    .filter(scoped)
                    .find(|o| o.id == id)
                    .ok_or_else(|| missing("environment operation in this worktree"))?
                    .clone();
                if matches!(command, Command::Cancel { .. }) && ACTIVE.contains(&o.state.as_str()) {
                    o.cancel = true;
                    store.save_environment_operation(&o)?;
                }
                Ok(o.view())
            }
            Command::Collect => {
                Ok(json!({"collected":self.collect(store,Some((project,&worktree)))?}))
            }
            command => {
                let package = Package::load(store)?;
                let (key, kind, template, inputs, workflow) = match command {
                    Command::Initialize { key, template } => {
                        let t = package.template(&template)?;
                        (key, "initialize", template, t.inputs.clone(), None)
                    }
                    Command::Prepare { key, expected } => {
                        let template = package
                            .templates
                            .iter()
                            .find(|(_, t)| t.inputs == expected)
                            .map(|(n, _)| n.clone())
                            .unwrap_or_else(|| "base".into());
                        let custom = package.template(&template)?.inputs != expected;
                        (
                            key,
                            "prepare",
                            template,
                            expected,
                            custom.then_some(Workflow {
                                change: Change::Sync,
                                offline: true,
                            }),
                        )
                    }
                    Command::Manage {
                        key,
                        expected,
                        change,
                        offline,
                    } => (
                        key,
                        "manage",
                        "base".into(),
                        expected,
                        Some(Workflow { change, offline }),
                    ),
                    _ => unreachable!(),
                };
                crate::notebooks::contract::key(&key)?;
                if let Some(o) = operations.iter().filter(scoped).find(|o| o.key == key) {
                    if o.kind != kind
                        || o.inputs != inputs
                        || o.contract != package.identity
                        || o.template != template
                        || o.workflow != workflow
                    {
                        return Err(conflict("environment request key has different parameters"));
                    }
                    return Ok(o.view());
                }
                if let Some(workflow) = &workflow {
                    transaction::validate_change(&worktree, &workflow.change)?;
                }
                if operations
                    .iter()
                    .filter(|o| ACTIVE.contains(&o.state.as_str()))
                    .count()
                    >= self.limits.queue
                    || operations
                        .iter()
                        .filter(scoped)
                        .any(|o| ACTIVE.contains(&o.state.as_str()))
                {
                    return Err(conflict(
                        "environment preparation queue or worktree is occupied",
                    ));
                }
                if kind != "initialize" && files::inputs(&worktree)? != inputs {
                    return Err(conflict(
                        "environment inputs changed; inspect before preparing",
                    ));
                }
                if kind != "initialize"
                    && files::free_bytes(store.root())? < self.limits.admission_free
                {
                    return Err(conflict("environment preparation requires 2 GiB free disk"));
                }
                let o = Operation {
                    id: OperationId::new(),
                    project,
                    worktree,
                    key,
                    state: "queued".into(),
                    kind: kind.into(),
                    inputs,
                    template,
                    contract: package.identity,
                    generation: None,
                    created: now(),
                    started: None,
                    worker: None,
                    error: None,
                    cancel: false,
                    workflow,
                    result: None,
                    publication: None,
                };
                store.save_environment_operation(&o)?;
                Ok(o.view())
            }
        }
    }
    pub fn tick(&mut self, store: &mut Store, stopping: bool) -> Result<bool> {
        let operations = store.environment_operations()?;
        // One global writer of the cache and materializations, even when
        // requests from multiple worktrees arrive before a maintenance tick.
        for mut o in operations
            .into_iter()
            .filter(|o| ACTIVE.contains(&o.state.as_str()))
        {
            if stopping {
                o.cancel = true;
                store.save_environment_operation(&o)?;
            }
            if o.cancel {
                self.fail(store, &mut o, "cancelled", "cancelled")?;
                continue;
            }
            if o.state == "queued" {
                if self.child.is_some() {
                    continue;
                }
                if let Err(error) = self.start(store, &mut o) {
                    self.fail(store, &mut o, "failed", &diagnostic(error))?;
                }
            } else if let Err(error) = self.poll(store, &mut o) {
                self.fail(store, &mut o, "failed", &diagnostic(error))?;
            }
        }
        if stopping && self.child.is_none() {
            store.clear_environment_leases()?;
            return Ok(true);
        }
        Ok(false)
    }
    fn operation_dir(store: &Store, id: OperationId) -> Result<PathBuf> {
        let root = store.root().join("notebook-environment-work");
        files::directory(&root)?;
        let dir = root.join(id.to_string());
        files::directory(&dir)?;
        Ok(dir)
    }
    fn start(&mut self, store: &mut Store, o: &mut Operation) -> Result<()> {
        Binding {
            project_id: o.project,
            worktree: o.worktree.clone(),
        }
        .validate(store)?;
        if o.worktree.canonicalize()? != o.worktree {
            return Err(conflict("environment worktree path changed"));
        }
        let package = Package::load(store)?;
        if package.identity != o.contract {
            return Err(conflict("environment component changed"));
        }
        let template = package.template(&o.template)?;
        o.started = Some(now());
        if o.kind == "initialize" {
            o.state = "initializing".into();
            store.save_environment_operation(o)?;
            files::initialize(o, template, &package)?;
            o.state = "ready".into();
            store.save_environment_operation(o)?;
            return Ok(());
        }
        if files::inputs(&o.worktree)? != o.inputs
            || files::free_bytes(store.root())? < self.limits.admission_free
        {
            return Err(conflict("environment inputs or free disk changed"));
        }
        // The interpreter and worker are the trust boundary for validation of
        // the larger wheel inventory. Check their bytes before opening the gate.
        package.verify_executable("python/runtime/bin/python3.12")?;
        package.verify_executable("python/notebooks/environment-worker.py")?;
        if o.workflow.is_some() {
            package.verify_executable("python/notebooks/environment-packages.py")?;
        }
        let key = hash(json!([o.project, o.worktree]).to_string().as_bytes());
        let parent = store.root().join("notebook-environments");
        files::directory(&parent)?;
        let parent = parent.join(&key);
        files::directory(&parent)?;
        let id = OperationId::new();
        let path = parent.join(id.to_string());
        // Persist ownership before mkdir. A crash in this gap is an invalid
        // generation, never something ready or eligible to execute.
        let mut g = Generation {
            id,
            project: o.project,
            worktree: o.worktree.clone(),
            worktree_key: key,
            path: path.clone(),
            directory: (0, 0),
            inputs: o.inputs.clone(),
            contract: o.contract.clone(),
            interpreter: package.root.join("python/runtime/bin/python3.12"),
            installation: package.installation.clone(),
            state: "building".into(),
            inventory: None,
        };
        store.save_environment_generation(&g)?;
        o.generation = Some(id);
        o.state = "preparing".into();
        store.save_environment_operation(o)?;
        g.directory = files::directory(&path)?;
        store.save_environment_generation(&g)?;
        let dir = Self::operation_dir(store, o.id)?;
        let cache = store.root().join("notebook-environment-cache");
        files::directory(&cache)?;
        let home = dir.join("home");
        files::directory(&home)?;
        if let Some(workflow) = &o.workflow {
            transaction::snapshot(o, workflow, &dir)?;
            files::directory(&store.root().join("notebook-environment-artifacts"))?;
        }
        let config = dir.join("prepare.json");
        supervisor::write_json(
            &config,
            &json!({"version":1,"package":package.root,"contract":package.identity,
            "generation":g.path,"directory":g.directory,"template":o.template,"cache":cache,"home":home,
            "report":dir.join("result.json"),"progress":dir.join("progress.json"),"max_bytes":self.limits.generation_bytes,
            "reserve_bytes":self.limits.reserve,"cache_bytes":self.limits.cache_bytes,
            "operation_id":o.id,"workflow":o.workflow,"documents":dir.join("documents"),"artifacts":store.root().join("notebook-environment-artifacts")}),
        )?;
        let launch = Launch {
            root: store.root().into(),
            generation: store.generation(),
            role: format!("environment-{}", o.id),
            token: crate::console::secret()?,
            branch: None,
            argv: vec![
                g.interpreter.to_string_lossy().into(),
                "-I".into(),
                "-B".into(),
                package
                    .root
                    .join("python/notebooks/environment-worker.py")
                    .to_string_lossy()
                    .into(),
                config.to_string_lossy().into(),
            ],
            env: BTreeMap::from([
                ("PATH".into(), "/usr/bin:/bin".into()),
                ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
                ("HOME".into(), home.to_string_lossy().into()),
            ]),
            cwd: dir.clone(),
        };
        let child = supervisor::start_owned_before(
            store,
            &launch,
            &dir.join("launch.json"),
            &dir.join("worker.log"),
            |store, record| {
                o.worker = Some(record.clone());
                store.save_environment_operation(o)
            },
        )?;
        self.child = Some((o.id, child));
        Ok(())
    }
    fn poll(&mut self, store: &mut Store, o: &mut Operation) -> Result<()> {
        let worker = o
            .worker
            .as_ref()
            .ok_or_else(|| conflict("missing environment process ownership"))?;
        let rss = supervisor::members(worker)?
            .into_iter()
            .try_fold(0u64, |sum, pid| {
                Ok::<_, crate::store::Error>(sum.saturating_add(supervisor::os::rss(pid)?))
            })?;
        if now() - o.started.unwrap_or(0)
            > self.limits.deadline_ms * if o.workflow.is_some() { 3 } else { 1 }
            || rss > self.limits.rss
            || files::free_bytes(store.root())? < self.limits.reserve
        {
            return Err(conflict("environment preparation resource limit"));
        }
        let dir = Self::operation_dir(store, o.id)?;
        if o.workflow.is_some() {
            files::bounded_size(&dir, 3 * 1024 * 1024 * 1024)?;
            files::bounded_size(
                &store.root().join("notebook-environment-cache"),
                self.limits.cache_bytes,
            )?;
            files::bounded_size(
                &store.root().join("notebook-environment-artifacts"),
                self.limits.cache_bytes,
            )?;
        }
        if let Ok(bytes) = files::read(&dir.join("progress.json"), 4096) {
            let progress: Value = serde_json::from_slice(&bytes)?;
            if progress["stage"] == "verifying" && o.state != "verifying" {
                o.state = "verifying".into();
                store.save_environment_operation(o)?;
            }
        }
        let (id, child) = self
            .child
            .as_mut()
            .ok_or_else(|| conflict("environment process handle missing"))?;
        if *id != o.id {
            return Err(conflict("environment process handle differs"));
        }
        let Some(status) = child.try_wait()? else {
            return Ok(());
        };
        supervisor::stop(worker)?;
        store.forget_native_process(worker)?;
        self.child = None;
        o.worker = None;
        if !status.success() {
            let message = files::read(&dir.join("error.json"), 4096)
                .ok()
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                .and_then(|v| v["code"].as_str().map(String::from));
            return Err(conflict(match message.as_deref() {
                Some("offline_artifacts_missing") => {
                    "offline wheel artifacts missing; import a bundle or run env sync online"
                }
                Some("resolution_failed") => {
                    "dependency resolution failed; check Python and protected package constraints in env status (private worker.log has details)"
                }
                Some("invalid_declaration") => {
                    "unsupported declaration or kernel dependency conflict; use registry requirements compatible with env status"
                }
                Some("invalid_bundle") => "bundle failed hash, target, path or size validation",
                Some("network_failed") => {
                    "package download failed; retry with a new request key or import an offline bundle"
                }
                _ => "environment worker failed; inspect the private operation worker.log",
            }));
        }
        let package = Package::load(store)?;
        Binding {
            project_id: o.project,
            worktree: o.worktree.clone(),
        }
        .validate(store)?;
        if o.worktree.canonicalize()? != o.worktree {
            return Err(conflict(
                "environment worktree path changed before publication",
            ));
        }
        if package.identity != o.contract || files::inputs(&o.worktree)? != o.inputs {
            return Err(conflict("environment inputs changed before publication"));
        }
        let mut g = store
            .environment_generations()?
            .into_iter()
            .find(|g| Some(g.id) == o.generation)
            .ok_or_else(|| missing("prepared generation"))?;
        files::generation_path(store, &g, false)?;
        let report: Value =
            serde_json::from_slice(&files::read(&dir.join("result.json"), 1024 * 1024)?)?;
        if report["status"] != "ready"
            || report["contract"] != g.contract
            || report["prefix"] != json!(g.path)
            || (o.workflow.is_none()
                && report["packages"] != json!(package.template(&o.template)?.packages))
            || report["inventory"].as_str().is_none_or(|s| s.len() != 64)
        {
            return Err(conflict("environment verification report differs"));
        }
        if o.workflow.is_some() {
            for (name, version) in &package.template("base")?.packages {
                if report["packages"][name] != *version {
                    return Err(conflict("prepared environment violates kernel contract"));
                }
            }
            let published = transaction::result_inputs(&dir)?;
            g.inputs = published.clone();
            o.result = Some(
                json!({"packages":report["packages"],"changes":report["changes"],
                "network":report["network"],"adoption_required":true,"bundle":report["bundle"]}),
            );
            o.publication = Some(published);
            // Journal the validated result before any project filesystem changes.
            g.inventory = report["inventory"].as_str().map(String::from);
            store.save_environment_generation(&g)?;
            store.save_environment_operation(o)?;
            transaction::publish(o, &dir)?;
            transaction::export(o)?;
        }
        g.inventory = report["inventory"].as_str().map(String::from);
        g.state = "ready".into();
        o.state = "ready".into();
        // One FULL-synchronous SQLite transaction is the activation point. A
        // report file alone never authorizes a kernel or changes active state.
        store.publish_environment(o, &g)?;
        if o.workflow.is_some()
            && let Err(error) = transaction::cleanup(&dir)
        {
            self.last_error = Some(diagnostic(error));
        }
        Ok(())
    }
    fn fail(
        &mut self,
        store: &mut Store,
        o: &mut Operation,
        state: &str,
        reason: &str,
    ) -> Result<()> {
        if let Some(worker) = &o.worker {
            supervisor::stop(worker)?;
            store.forget_native_process(worker)?;
        }
        if self.child.as_ref().is_some_and(|(id, _)| *id == o.id) {
            let (_, mut child) = self.child.take().unwrap();
            child.wait()?;
        }
        o.worker = None;
        o.state = state.into();
        o.error = Some(reason.into());
        store.save_environment_operation(o)?;
        if let Some(mut g) = store
            .environment_generations()?
            .into_iter()
            .find(|g| Some(g.id) == o.generation && g.state == "building")
        {
            g.state = "invalid".into();
            store.invalidate_environment(&g)?;
        }
        if o.workflow.is_some() {
            let dir = store
                .root()
                .join("notebook-environment-work")
                .join(o.id.to_string());
            if dir.exists()
                && let Err(error) = transaction::cleanup(&dir)
            {
                self.last_error = Some(diagnostic(error));
            }
        }
        Ok(())
    }
    pub fn acquire(
        &self,
        store: &mut Store,
        binding: &Binding,
        generation: OperationId,
        holder: &str,
    ) -> Result<OperationId> {
        binding.validate(store)?;
        crate::notebooks::contract::key(holder)?;
        let g = store
            .environment_generations()?
            .into_iter()
            .find(|g| {
                g.id == generation
                    && g.project == binding.project_id
                    && g.worktree == binding.worktree
                    && g.state == "ready"
            })
            .ok_or_else(|| missing("ready environment in this worktree"))?;
        files::generation_path(store, &g, false)?;
        let package = Package::load(store)?;
        if g.contract != package.identity || g.installation != package.installation {
            return Err(conflict(
                "environment interpreter or contract changed; prepare again",
            ));
        }
        store.lease_environment(g.id, holder)
    }
    pub fn release(&self, store: &Store, lease: OperationId) -> Result<()> {
        store.release_environment_lease(lease)
    }
    fn collect(
        &self,
        store: &mut Store,
        scope: Option<(ProjectId, &Path)>,
    ) -> Result<Vec<OperationId>> {
        let mut collected = Vec::new();
        for mut g in store.environment_generations()? {
            if scope.is_some_and(|(p, w)| p != g.project || w != g.worktree)
                || !["ready", "invalid", "deleting"].contains(&g.state.as_str())
            {
                continue;
            }
            if g.state == "deleting" && files::deletion_finished(store, &g).unwrap_or(false) {
                g.state = "removed".into();
                store.save_environment_generation(&g)?;
                collected.push(g.id);
                continue;
            }
            // Validate before committing deletion and again before touching the
            // tree. Substituted roots are quarantined for inspection, not erased.
            let path = match files::generation_path(store, &g, false) {
                Ok(p) => p,
                Err(_) => continue,
            };
            g.state = "deleting".into();
            if !store.collect_environment(&g)? {
                continue;
            }
            files::generation_path(store, &g, false)?;
            fs::remove_dir_all(&path)?;
            fs::File::open(path.parent().unwrap())?.sync_all()?;
            g.state = "removed".into();
            store.save_environment_generation(&g)?;
            collected.push(g.id);
        }
        Ok(collected)
    }
}
