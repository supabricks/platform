//! Browser project packaging. Paths are selected by the daemon, never by HTTP callers.
use crate::{
    api::Binding,
    deployments::{self, Source},
    project_apply, projects,
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{Read, Seek, SeekFrom, Write},
    path::PathBuf,
    time::{Duration, Instant},
};
use supabricks_core::resource::{DeploymentId, OperationId};

const MAX_BYTES: u64 = 300 * 1024 * 1024;
const CHUNK: usize = 24576;
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Selection {
    Current,
    Imported { id: OperationId },
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    List,
    View {
        target: Option<String>,
    },
    Begin {
        bytes: u64,
    },
    Chunk {
        id: OperationId,
        offset: u64,
        hex: String,
    },
    Verify {
        id: OperationId,
        target: Option<String>,
    },
    Dispose {
        id: OperationId,
    },
    Unpack {
        id: OperationId,
        destination: OperationId,
        target: Option<String>,
    },
    Export {
        target: Option<String>,
    },
    Download {
        id: OperationId,
        offset: u64,
    },
    Bind {
        key: String,
        target: Option<String>,
    },
    Attach {
        deployment: DeploymentId,
    },
    Apply {
        command: project_apply::Command,
    },
    Reopen {
        environment: Option<String>,
    },
}
struct Transfer {
    owner: String,
    file: tempfile::NamedTempFile,
    expected: u64,
    received: u64,
    export: bool,
    touched: Instant,
}
#[derive(Default)]
pub(crate) struct Workspace {
    transfers: BTreeMap<String, Transfer>,
}
fn home(store: &Store, host: &Binding) -> Result<PathBuf> {
    let base = store.root().join("console-projects");
    super::directory(&base)?;
    let path = base.join(host.project_id.to_string());
    super::directory(&path)?;
    // Publication syncs this directory; persist its newly created ancestors too.
    std::fs::File::open(store.root())?.sync_all()?;
    std::fs::File::open(&base)?.sync_all()?;
    Ok(path)
}
pub(crate) fn source(store: &Store, host: &Binding, selected: &Selection) -> Result<Source> {
    let path = match selected {
        Selection::Current => host.worktree.clone(),
        Selection::Imported { id } => {
            let path = home(store, host)?.join(id.to_string());
            // Reject replaced/symlinked worktrees before resolving public identity.
            let m = std::fs::symlink_metadata(&path)?;
            if !m.is_dir() {
                return Err(invalid("imported project must be a directory, not a link"));
            }
            path
        }
    };
    Source::read(&path)
}
fn binding(store: &mut Store, source: &Source) -> Result<Binding> {
    let context: deployments::Context =
        serde_json::from_value(store.project_command(source, deployments::Command::Inspect)?)?;
    Ok(context.binding(&source.worktree))
}
pub(crate) fn reopen(
    store: &mut Store,
    host: &Binding,
    selected: &Selection,
    environment: Option<String>,
) -> Result<Binding> {
    let source = source(store, host, selected)?;
    let bound = binding(store, &source)?;
    if let Some(environment) = environment {
        let installed = project_apply::handle(store, &bound, project_apply::Command::Installed)?;
        let path = installed["environment_worktrees"][&environment]
            .as_str()
            .ok_or_else(|| invalid("installed environment not found; apply the project first"))?;
        let path = PathBuf::from(path);
        let context = store.binding_context(&bound)?;
        let result = context.binding(&path);
        result.validate(store)?;
        Ok(result)
    } else {
        Ok(bound)
    }
}
impl Workspace {
    pub fn recover(store: &Store) -> Result<Self> {
        let tmp = store.root().join("tmp");
        super::directory(&tmp)?;
        for entry in std::fs::read_dir(tmp)? {
            let entry = entry?;
            if entry
                .file_name()
                .to_string_lossy()
                .starts_with("console-package-")
                && entry.file_type()?.is_file()
            {
                std::fs::remove_file(entry.path())?;
            }
        }
        Ok(Self::default())
    }

    fn transfer(&mut self, owner: &str, id: OperationId) -> Result<&mut Transfer> {
        let t = self
            .transfers
            .get_mut(&id.to_string())
            .filter(|t| t.owner == owner)
            .ok_or_else(|| {
                conflict(
                    "package transfer expired or belongs to another session; select the file again",
                )
            })?;
        t.touched = Instant::now();
        Ok(t)
    }
    fn allocate(
        &mut self,
        store: &Store,
        owner: &str,
        bytes: u64,
        export: bool,
    ) -> Result<OperationId> {
        if bytes == 0 || bytes > MAX_BYTES {
            return Err(invalid("package must contain 1 byte to 300 MiB"));
        }
        if self.transfers.len() >= 4
            || self.transfers.values().filter(|t| t.owner == owner).count() >= 2
        {
            return Err(conflict(
                "package transfer capacity reached; discard a transfer first",
            ));
        }
        let tmp = store.root().join("tmp");
        super::directory(&tmp)?;
        let file = tempfile::Builder::new()
            .prefix("console-package-")
            .tempfile_in(tmp)?;
        super::ingestion::reserve(file.as_file(), bytes)?;
        let id = OperationId::new();
        self.transfers.insert(
            id.to_string(),
            Transfer {
                owner: owner.into(),
                file,
                expected: bytes,
                received: 0,
                export,
                touched: Instant::now(),
            },
        );
        Ok(id)
    }
    pub fn handle(
        &mut self,
        store: &mut Store,
        host: &Binding,
        owner: &str,
        selected: Selection,
        command: Command,
    ) -> Result<Value> {
        self.transfers
            .retain(|_, t| t.touched.elapsed() < Duration::from_secs(900));
        match command {
            Command::Begin { bytes } => {
                let id = self.allocate(store, owner, bytes, false)?;
                return Ok(json!({"id":id,"bytes":bytes,"chunk_bytes":CHUNK}));
            }
            Command::Chunk { id, offset, hex } => {
                if hex.len() > CHUNK * 2 {
                    return Err(invalid("package chunk exceeds 24 KiB"));
                }
                let bytes = hex::decode(hex).map_err(|_| invalid("invalid package chunk"))?;
                let t = self.transfer(owner, id)?;
                if t.export
                    || offset != t.received
                    || bytes.is_empty()
                    || t.received + bytes.len() as u64 > t.expected
                {
                    return Err(conflict(
                        "package chunk is out of order or exceeds the declared size; select the file again",
                    ));
                }
                super::ingestion::reserve(t.file.as_file(), bytes.len() as u64)?;
                t.file.write_all(&bytes)?;
                t.received += bytes.len() as u64;
                return Ok(json!({"received":t.received}));
            }
            Command::Dispose { id } => {
                self.transfer(owner, id)?;
                self.transfers.remove(&id.to_string());
                return Ok(json!({"disposed":true}));
            }
            Command::Verify { id, target } => {
                let t = self.transfer(owner, id)?;
                if t.received != t.expected || t.export {
                    return Err(conflict("package upload is incomplete"));
                }
                return Ok(json!(projects::package::verify(
                    t.file.path(),
                    target.as_deref()
                )?));
            }
            Command::Unpack {
                id,
                destination,
                target,
            } => {
                let base = home(store, host)?;
                let path = base.join(destination.to_string());
                let t = self.transfer(owner, id)?;
                if t.received != t.expected || t.export {
                    return Err(conflict("package upload is incomplete"));
                }
                let report = projects::package::verify(t.file.path(), target.as_deref())?;
                if path.try_exists()? {
                    // Lost reply retries only acknowledge our original published archive.
                    let existing = source(store, host, &Selection::Imported { id: destination })?;
                    use crate::notebooks::files::directory::Directory;
                    let dir = Directory::project(&existing.worktree)?;
                    let file = dir.open(
                        std::ffi::OsStr::new("supabricks-unpacked.json"),
                        libc::O_RDONLY,
                    )?;
                    let marker: Value = serde_json::from_reader(file.take(4096))?;
                    if marker["archive_sha256"] != report.archive_sha256 {
                        return Err(conflict(
                            "destination already contains another package; choose a new import",
                        ));
                    }
                } else {
                    if std::fs::read_dir(&base)?.count() >= 32 {
                        return Err(conflict(
                            "32 imported projects already exist; manage their directories with the CLI",
                        ));
                    }
                    projects::package::unpack(t.file.path(), &path, target.as_deref())?;
                }
                return Ok(
                    json!({"source":{"kind":"imported","id":destination},"worktree":path,"report":report}),
                );
            }
            Command::Download { id, offset } => {
                let t = self.transfer(owner, id)?;
                if !t.export || offset > t.expected {
                    return Err(invalid("invalid package download offset"));
                }
                t.file.seek(SeekFrom::Start(offset))?;
                let mut bytes = vec![0; CHUNK.min((t.expected - offset) as usize)];
                t.file.read_exact(&mut bytes)?;
                return Ok(json!({"hex":hex::encode(bytes),"bytes":t.expected}));
            }
            Command::List => {
                let mut entries = Vec::new();
                for entry in std::fs::read_dir(home(store, host)?)?.take(33) {
                    let entry = entry?;
                    let Ok(id) = entry.file_name().to_string_lossy().parse::<OperationId>() else {
                        continue;
                    };
                    let selected = Selection::Imported { id };
                    if let Ok(s) = source(store, host, &selected) {
                        let identity = projects::source_identity(&s.worktree)?;
                        entries.push(json!({"source":selected,"name":identity.name,"definition_id":identity.id,"worktree":s.worktree}));
                    }
                }
                return Ok(json!({"imports":entries}));
            }
            _ => {}
        }
        let source = source(store, host, &selected)?;
        match command {
            Command::View { target } => {
                let context = store.project_command(&source, deployments::Command::Inspect);
                let binding_error = context.as_ref().err().map(crate::client::diagnostic);
                let context = context.ok();
                let selected_target = context
                    .as_ref()
                    .and_then(|v| v["target"].as_str())
                    .or(target.as_deref());
                let inspection = projects::inspect(&source.worktree, selected_target);
                let source_error = inspection.as_ref().err().map(crate::client::diagnostic);
                let inspection = inspection.ok();
                let installed = if context.is_some() {
                    let bound = binding(store, &source)?;
                    Some(project_apply::handle(
                        store,
                        &bound,
                        project_apply::Command::Installed,
                    )?)
                } else {
                    None
                };
                let installed_source = installed
                    .as_ref()
                    .and_then(|i| i["active_revision"].as_str())
                    .and_then(|id| id.parse().ok())
                    .map(|id| {
                        let c: deployments::Context =
                            serde_json::from_value(context.clone().unwrap())?;
                        Ok::<_, crate::store::Error>(
                            store.project_apply(c.deployment_id, id)?.plan.source_sha256,
                        )
                    })
                    .transpose()?;
                let operation = context
                    .as_ref()
                    .map(|v| {
                        let c: deployments::Context = serde_json::from_value(v.clone())?;
                        store.latest_deployment_apply(c.deployment_id)
                    })
                    .transpose()?
                    .flatten();
                let deployments = store.project_command(&source, deployments::Command::List)?;
                Ok(
                    json!({"operation":operation,"worktree":source.worktree,"inspection":inspection,"source_error":source_error,"context":context,"binding_error":binding_error,"installed":installed,"installed_source_sha256":installed_source,"deployments":deployments["deployments"]}),
                )
            }
            Command::Export { target } => {
                let p = projects::package::prepare(&source.worktree, target.as_deref())?;
                let id = self.allocate(store, owner, p.archive.len() as u64, true)?;
                let t = self.transfer(owner, id)?;
                t.file.write_all(&p.archive)?;
                t.received = t.expected;
                Ok(json!({"id":id,"bytes":t.expected,"chunk_bytes":CHUNK,"report":p.report}))
            }
            Command::Bind { key, target } => {
                store.project_command(&source, deployments::Command::Create { key, target })
            }
            Command::Attach { deployment } => {
                store.project_command(&source, deployments::Command::Attach { deployment })
            }
            Command::Apply { command } => {
                let bound = binding(store, &source)?;
                project_apply::handle(store, &bound, command)
            }
            Command::Reopen { .. } => {
                Err(invalid("reopen must be dispatched by the console manager"))
            }
            _ => unreachable!(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use supabricks_core::resource::ProjectId;
    struct Fixture {
        _temp: tempfile::TempDir,
        store: Store,
        host: Binding,
        ui: Workspace,
    }
    impl Fixture {
        fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            let path = temp.path().join("source");
            fs::create_dir(&path).unwrap();
            fs::write(path.join("supabricks.toml"),format!("format_version=2\nid='{}'\nname='browser-project'\n[package]\nversion='1.0.0'\ninclude=[]\nnotebook_outputs='strip'\n",ProjectId::new())).unwrap();
            let mut store = Store::open(&temp.path().join("data")).unwrap();
            let src = Source::read(&path).unwrap();
            let ctx: deployments::Context = serde_json::from_value(
                store
                    .project_command(
                        &src,
                        deployments::Command::Create {
                            key: "host".into(),
                            target: None,
                        },
                    )
                    .unwrap(),
            )
            .unwrap();
            Self {
                _temp: temp,
                store,
                host: ctx.binding(&src.worktree),
                ui: Workspace::default(),
            }
        }
        fn call(&mut self, owner: &str, selected: Selection, c: Command) -> Result<Value> {
            self.ui
                .handle(&mut self.store, &self.host, owner, selected, c)
        }
        fn upload(&mut self, bytes: &[u8]) -> OperationId {
            let slot = self
                .call(
                    "a",
                    Selection::Current,
                    Command::Begin {
                        bytes: bytes.len() as u64,
                    },
                )
                .unwrap();
            let id = serde_json::from_value(slot["id"].clone()).unwrap();
            for (i, chunk) in bytes.chunks(CHUNK).enumerate() {
                self.call(
                    "a",
                    Selection::Current,
                    Command::Chunk {
                        id,
                        offset: (i * CHUNK) as u64,
                        hex: hex::encode(chunk),
                    },
                )
                .unwrap();
            }
            id
        }
    }
    #[test]
    fn transfer_admission_is_session_bound_bounded_and_ordered() {
        let mut f = Fixture::new();
        assert!(
            f.call(
                "a",
                Selection::Current,
                Command::Begin {
                    bytes: MAX_BYTES + 1
                }
            )
            .is_err()
        );
        let id = f.upload(b"broken package");
        assert!(
            f.call(
                "b",
                Selection::Current,
                Command::Verify { id, target: None }
            )
            .is_err()
        );
        assert!(
            f.call(
                "a",
                Selection::Current,
                Command::Verify { id, target: None }
            )
            .is_err()
        );
        assert!(
            f.call(
                "a",
                Selection::Current,
                Command::Chunk {
                    id,
                    offset: 0,
                    hex: "00".into()
                }
            )
            .is_err()
        );
        assert!(
            f.call(
                "a",
                Selection::Current,
                Command::Unpack {
                    id,
                    destination: OperationId::new(),
                    target: None
                }
            )
            .is_err()
        );
        assert_eq!(
            fs::read_dir(home(&f.store, &f.host).unwrap())
                .unwrap()
                .count(),
            0
        );
        f.call("a", Selection::Current, Command::Dispose { id })
            .unwrap();
        assert!(
            f.call(
                "a",
                Selection::Current,
                Command::Verify { id, target: None }
            )
            .is_err()
        );
    }
    #[test]
    fn unpack_retry_remains_unbound_and_cli_binding_is_identical() {
        let mut f = Fixture::new();
        let p = projects::package::prepare(&f.host.worktree, None).unwrap();
        let id = f.upload(&p.archive);
        let destination = OperationId::new();
        for _ in 0..2 {
            f.call(
                "a",
                Selection::Current,
                Command::Unpack {
                    id,
                    destination,
                    target: None,
                },
            )
            .unwrap();
        }
        let selection = Selection::Imported { id: destination };
        let before = f
            .call("a", selection.clone(), Command::View { target: None })
            .unwrap();
        assert!(before["context"].is_null());
        assert_eq!(
            before["inspection"]["definition"]["id"],
            json!(Source::read(&f.host.worktree).unwrap().definition_id)
        );
        let context = f
            .call(
                "a",
                selection.clone(),
                Command::Bind {
                    key: "new".into(),
                    target: None,
                },
            )
            .unwrap();
        assert_ne!(context["runtime_project_id"], json!(f.host.project_id));
        let src = source(&f.store, &f.host, &selection).unwrap();
        assert_eq!(
            context,
            f.store
                .project_command(&src, deployments::Command::Inspect)
                .unwrap()
        );
        assert_eq!(
            context,
            f.call(
                "a",
                selection.clone(),
                Command::Bind {
                    key: "new".into(),
                    target: None
                }
            )
            .unwrap()
        );
        assert!(
            f.call(
                "a",
                selection.clone(),
                Command::Bind {
                    key: "different".into(),
                    target: None
                }
            )
            .is_err()
        );
        // A different deployment cannot silently take over the already-bound checkout.
        let host = f.store.binding_context(&f.host).unwrap();
        assert!(
            f.call(
                "a",
                selection.clone(),
                Command::Attach {
                    deployment: host.deployment_id
                }
            )
            .is_err()
        );
        let p: project_apply::Plan = serde_json::from_value(
            f.call(
                "a",
                selection.clone(),
                Command::Apply {
                    command: project_apply::Command::Plan {
                        options: Default::default(),
                    },
                },
            )
            .unwrap(),
        )
        .unwrap();
        fs::write(
            src.worktree.join("supabricks.toml"),
            fs::read_to_string(src.worktree.join("supabricks.toml"))
                .unwrap()
                .replace("1.0.0", "1.0.1"),
        )
        .unwrap();
        assert!(
            f.call(
                "a",
                selection,
                Command::Apply {
                    command: project_apply::Command::Apply {
                        plan: p,
                        key: "stale".into()
                    }
                }
            )
            .is_err()
        );
    }
    #[test]
    fn export_is_identical_to_cli_and_import_path_cannot_escape() {
        let mut f = Fixture::new();
        let expected = projects::package::prepare(&f.host.worktree, None).unwrap();
        let e = f
            .call("a", Selection::Current, Command::Export { target: None })
            .unwrap();
        let id = serde_json::from_value(e["id"].clone()).unwrap();
        let mut bytes = Vec::new();
        while bytes.len() < expected.archive.len() {
            let chunk = f
                .call(
                    "a",
                    Selection::Current,
                    Command::Download {
                        id,
                        offset: bytes.len() as u64,
                    },
                )
                .unwrap();
            bytes.extend(hex::decode(chunk["hex"].as_str().unwrap()).unwrap());
        }
        assert_eq!(bytes, expected.archive);
        assert!(
            f.call(
                "other",
                Selection::Current,
                Command::Download { id, offset: 0 }
            )
            .is_err()
        );
        let fake = OperationId::new();
        let root = home(&f.store, &f.host).unwrap();
        std::os::unix::fs::symlink(&f.host.worktree, root.join(fake.to_string())).unwrap();
        assert!(source(&f.store, &f.host, &Selection::Imported { id: fake }).is_err());
        assert!(
            serde_json::from_value::<Selection>(json!({"kind":"imported","id":"../../outside"}))
                .is_err()
        );
        assert!(
            serde_json::from_value::<Command>(
                json!({"action":"unpack","id":id,"destination":fake,"path":"/tmp/overwrite"})
            )
            .is_err()
        );
    }
}
