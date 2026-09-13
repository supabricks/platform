//! Browser admission and bounded upload descriptors owned by the sole daemon.
use crate::{
    api::Binding,
    ingest::{self, JobId, Load, Mapping, SourceId},
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::File,
    io::Write,
    time::{Duration, Instant},
};
use supabricks_core::resource::ProjectId;

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Begin {
        name: String,
        bytes: u64,
    },
    Chunk {
        source: SourceId,
        offset: u64,
        hex: String,
    },
    Inspect {
        source: SourceId,
        mapping: Mapping,
    },
    Sources,
    Source {
        source: SourceId,
    },
    Dispose {
        source: SourceId,
    },
    Load {
        load: Load,
        key: String,
        preview: u64,
    },
    List,
    Status {
        id: JobId,
    },
    Cancel {
        id: JobId,
    },
    Retry {
        id: JobId,
    },
}
struct Slot {
    owner: String,
    project: ProjectId,
    file: Option<File>,
    expected: u64,
    received: u64,
    started: Instant,
    touched: Instant,
    preview: u64,
}
#[derive(Default)]
pub(crate) struct Uploads {
    slots: BTreeMap<String, Slot>,
}
fn reserve(file: &File, bytes: u64) -> Result<()> {
    use std::os::fd::AsRawFd;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::fstatvfs(file.as_raw_fd(), stat.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let stat = unsafe { stat.assume_init() };
    if (stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64) < 64 * 1024 * 1024 + bytes {
        return Err(conflict("disk_reserve: free space below upload reserve"));
    }
    Ok(())
}
impl Uploads {
    fn slot(
        &mut self,
        store: &Store,
        project: ProjectId,
        owner: &str,
        id: SourceId,
    ) -> Result<&mut Slot> {
        let s = self
            .slots
            .get_mut(&id.to_string())
            .ok_or_else(|| conflict("source is not bound to this console session; upload again"))?;
        if s.owner != owner || s.project != project {
            return Err(conflict("source belongs to another console session"));
        }
        let source = store.ingest_source(project, id)?;
        if source.expires_at_ms <= chrono::Utc::now().timestamp_millis()
            || source.generation != store.generation()
        {
            return Err(conflict("source expired; upload again"));
        }
        Ok(s)
    }
    pub(crate) fn tick(&mut self, store: &mut Store, stopping: bool) -> Result<()> {
        let workers = store.native_processes()?;
        let ids: Vec<_> = self
            .slots
            .iter()
            .filter(|(id, s)| {
                store
                    .ingest_source(s.project, id.parse().unwrap())
                    .is_ok_and(|v| v.state == "receiving")
                    && !workers
                        .iter()
                        .any(|p| p.role == format!("ingest-source-{id}"))
                    && (stopping
                        || s.touched.elapsed() > Duration::from_secs(30)
                        || s.started.elapsed() > Duration::from_secs(600))
            })
            .map(|(id, _)| id.clone())
            .collect();
        for id in ids {
            let slot = self.slots.remove(&id).unwrap();
            drop(slot.file);
            store.abandon_source(slot.project, id.parse().unwrap())?;
            store.dispose_source(slot.project, id.parse().unwrap(), false)?;
        }
        self.slots.retain(|id, s| {
            store
                .ingest_source(s.project, id.parse().unwrap())
                .is_ok_and(|v| !matches!(v.state.as_str(), "disposed" | "expired"))
        });
        Ok(())
    }
    pub(crate) fn handle(
        &mut self,
        store: &mut Store,
        service: &mut ingest::service::Service,
        binding: &Binding,
        owner: &str,
        command: Command,
    ) -> Result<Value> {
        let project = binding.project_id;
        match command {
            Command::Begin { name, bytes } => {
                if bytes == 0 || bytes > ingest::SOURCE_BYTES {
                    return Err(invalid("select a nonempty CSV or TSV file up to 100 MiB"));
                }
                ingest::service::worker(store)?;
                if self.slots.values().filter(|s| s.file.is_some()).count() >= 2
                    || self.slots.len() >= 32
                {
                    return Err(conflict("upload slots busy; dispose an unused source"));
                }
                let source = store.acquire_source(project, &name)?;
                let file = match store.source_writer(project, source.id).and_then(|f| {
                    reserve(&f, bytes)?;
                    Ok(f)
                }) {
                    Ok(f) => f,
                    Err(e) => {
                        store.abandon_source(project, source.id)?;
                        store.dispose_source(project, source.id, false)?;
                        return Err(e);
                    }
                };
                self.slots.insert(
                    source.id.to_string(),
                    Slot {
                        owner: owner.into(),
                        project,
                        file: Some(file),
                        expected: bytes,
                        received: 0,
                        started: Instant::now(),
                        touched: Instant::now(),
                        preview: 0,
                    },
                );
                Ok(json!({"source":source,"received":0,"preview":0}))
            }
            Command::Chunk {
                source,
                offset,
                hex,
            } => {
                let slot = self.slot(store, project, owner, source)?;
                if hex.is_empty() || hex.len() > 49152 {
                    return Err(invalid("upload chunks require 1..24576 bytes"));
                }
                let bytes = hex::decode(hex).map_err(|_| invalid("invalid upload bytes"))?;
                if slot.received != offset || offset + bytes.len() as u64 > slot.expected {
                    return Err(conflict("upload offset or declared size differs"));
                }
                let file = slot
                    .file
                    .as_mut()
                    .ok_or_else(|| conflict("upload is closed"))?;
                reserve(file, bytes.len() as u64)?;
                if let Err(error) = file.write_all(&bytes) {
                    // A failed write may already have advanced the descriptor.
                    // Never allow a chunk retry against an uncertain offset.
                    drop(slot.file.take());
                    store.abandon_source(project, source)?;
                    store.dispose_source(project, source, false)?;
                    return Err(error.into());
                }
                slot.received += bytes.len() as u64;
                slot.touched = Instant::now();
                Ok(json!({"received":slot.received}))
            }
            Command::Inspect { source, mapping } => {
                mapping.fingerprint()?;
                let slot = self.slot(store, project, owner, source)?;
                if slot.received != slot.expected {
                    return Err(conflict("upload incomplete"));
                }
                if let Some(f) = slot.file.take() {
                    let synced = f.sync_all();
                    drop(f);
                    if let Err(error) = synced {
                        store.abandon_source(project, source)?;
                        store.dispose_source(project, source, false)?;
                        return Err(error.into());
                    }
                }
                let value = service.inspect_uploaded(store, binding, source, mapping)?;
                slot.preview += 1;
                Ok(json!({"status":value,"preview":slot.preview}))
            }
            Command::Sources => {
                let mut values = Vec::new();
                for (id, s) in &self.slots {
                    if s.owner == owner && s.project == project {
                        values.push(json!({"source":store.ingest_source(project,id.parse().unwrap())?,"received":s.received,"expected":s.expected,"preview":s.preview}));
                    }
                }
                Ok(json!(values))
            }
            Command::Source { source } => {
                let slot = self.slot(store, project, owner, source)?;
                Ok(
                    json!({"status":ingest::service::source_status(store,project,source)?,"received":slot.received,"expected":slot.expected,"preview":slot.preview}),
                )
            }
            Command::Dispose { source } => {
                let slot = self.slot(store, project, owner, source)?;
                drop(slot.file.take());
                service.cancel_source(store, source)?;
                store.abandon_source(project, source)?;
                store.dispose_source(project, source, false)?;
                self.slots.remove(&source.to_string());
                Ok(json!({"disposed":true}))
            }
            Command::Load { load, key, preview } => {
                if load.project_id != project {
                    return Err(invalid("import project differs from console"));
                }
                if let Some(old) = store.ingest_for_key(project, load.branch_id, &key)? {
                    if old.load != load {
                        return Err(conflict("request key already used with another import"));
                    }
                    return ingest::service::status(store, project, old.id);
                }
                let slot = self.slot(store, project, owner, load.source_id)?;
                if slot.file.is_some() || slot.preview != preview || preview == 0 {
                    return Err(conflict("stale preview; inspect and approve again"));
                }
                let status = ingest::service::source_status(store, project, load.source_id)?;
                let proposed: Mapping =
                    serde_json::from_value(status["inspection"]["mapping"].clone())
                        .map_err(|_| conflict("inspection is not ready"))?;
                if proposed.format != load.mapping.format
                    || proposed.delimiter != load.mapping.delimiter
                    || proposed.header != load.mapping.header
                    || proposed.null_strings != load.mapping.null_strings
                    || status["source"]["sha256"] != load.source_sha256
                {
                    return Err(conflict(
                        "parser or source changed; inspect and approve again",
                    ));
                }
                let job = store.create_ingest(&key, load)?;
                ingest::service::status(store, project, job.id)
            }
            Command::List => Ok(json!(store.ingest_list(project, None, 20)?)),
            Command::Status { id } => ingest::service::status(store, project, id),
            Command::Cancel { id } => {
                store.cancel_ingest(project, id)?;
                ingest::service::status(store, project, id)
            }
            Command::Retry { id } => {
                store.retry_ingest(project, id)?;
                ingest::service::status(store, project, id)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (tempfile::TempDir, Store, Binding, Uploads, SourceId) {
        let root = tempfile::tempdir().unwrap();
        let mut store = Store::open(&root.path().join("data")).unwrap();
        let project = crate::project::ProjectConfig {
            format_version: 1,
            id: ProjectId::new(),
            name: "upload".into(),
        };
        store.register_project(&project).unwrap();
        let binding = Binding {
            project_id: project.id,
            worktree: root.path().into(),
        };
        let source = store
            .acquire_source(project.id, "../../display.csv")
            .unwrap();
        let file = store.source_writer(project.id, source.id).unwrap();
        let mut uploads = Uploads::default();
        uploads.slots.insert(
            source.id.to_string(),
            Slot {
                owner: "session".into(),
                project: project.id,
                file: Some(file),
                expected: 8,
                received: 0,
                started: Instant::now(),
                touched: Instant::now(),
                preview: 0,
            },
        );
        (root, store, binding, uploads, source.id)
    }
    #[test]
    fn upload_binding_order_limits_and_idle_cleanup() {
        let (_root, mut store, binding, mut uploads, id) = fixture();
        let mut service = ingest::service::Service::default();
        let chunk = || Command::Chunk {
            source: id,
            offset: 0,
            hex: hex::encode(b"a,b\n"),
        };
        assert!(
            uploads
                .handle(&mut store, &mut service, &binding, "other", chunk())
                .is_err()
        );
        assert_eq!(
            uploads
                .handle(&mut store, &mut service, &binding, "session", chunk())
                .unwrap()["received"],
            4
        );
        assert!(
            uploads
                .handle(&mut store, &mut service, &binding, "session", chunk())
                .is_err()
        );
        assert!(
            uploads
                .handle(
                    &mut store,
                    &mut service,
                    &binding,
                    "session",
                    Command::Chunk {
                        source: id,
                        offset: 4,
                        hex: hex::encode(b"123456")
                    }
                )
                .is_err()
        );
        assert_eq!(
            std::fs::read(store.source_path(id, "part").unwrap()).unwrap(),
            b"a,b\n"
        );
        uploads.slots.get_mut(&id.to_string()).unwrap().touched =
            Instant::now() - Duration::from_secs(31);
        uploads.tick(&mut store, false).unwrap();
        assert_eq!(
            store.ingest_source(binding.project_id, id).unwrap().state,
            "disposed"
        );
        assert!(!store.source_path(id, "part").unwrap().exists());
    }
    #[test]
    fn expired_source_and_closed_incomplete_upload_cannot_be_admitted() {
        let (_root, mut store, binding, mut uploads, id) = fixture();
        let connection = rusqlite::Connection::open(store.root().join("state.sqlite3")).unwrap();
        connection
            .execute("UPDATE ingest_sources SET expires_at_ms=0", [])
            .unwrap();
        assert!(
            uploads
                .slot(&store, binding.project_id, "session", id)
                .is_err()
        );
        // A failed launch after closing the writer must still release its slot.
        let slot = uploads.slots.get_mut(&id.to_string()).unwrap();
        drop(slot.file.take());
        slot.touched = Instant::now() - Duration::from_secs(31);
        uploads.tick(&mut store, false).unwrap();
        assert!(!store.source_path(id, "part").unwrap().exists());
    }
    #[test]
    fn write_failure_closes_and_disposes_the_slot_before_any_retry() {
        let (_root, mut store, binding, mut uploads, id) = fixture();
        let slot = uploads.slots.get_mut(&id.to_string()).unwrap();
        // A real read-only descriptor produces a real I/O failure, without
        // mocking the store or weakening production admission.
        slot.file = Some(File::open(store.source_path(id, "part").unwrap()).unwrap());
        let mut service = ingest::service::Service::default();
        assert!(
            uploads
                .handle(
                    &mut store,
                    &mut service,
                    &binding,
                    "session",
                    Command::Chunk {
                        source: id,
                        offset: 0,
                        hex: hex::encode(b"a,b\n")
                    }
                )
                .is_err()
        );
        assert!(uploads.slots[&id.to_string()].file.is_none());
        assert_eq!(
            store.ingest_source(binding.project_id, id).unwrap().state,
            "disposed"
        );
        assert!(!store.source_path(id, "part").unwrap().exists());
    }
    #[test]
    fn no_browser_command_can_accept_a_server_path() {
        assert!(
            serde_json::from_value::<Command>(
                json!({"action":"begin","name":"x.csv","bytes":8,"path":"/etc/passwd"})
            )
            .is_err()
        );
        assert!(
            serde_json::from_value::<Command>(
                json!({"action":"inspect","source":"../../etc/passwd"})
            )
            .is_err()
        );
    }
}
