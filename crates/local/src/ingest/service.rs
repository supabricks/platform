//! Asynchronous owned workers; the daemon is the only metadata writer.
use super::*;
use crate::{
    api::Binding,
    store::{
        Store,
        error::{conflict, invalid},
    },
    supervisor::{self, Launch, OwnedProcess},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Path, PathBuf},
    process::Child,
    time::{Duration, Instant},
};
use supabricks_core::resource::DesiredState;
#[derive(Default)]
pub(crate) struct Service {
    tasks: BTreeMap<String, Task>,
    retry_at: BTreeMap<String, Instant>,
}
struct Task {
    child: Child,
    process: OwnedProcess,
    project: ProjectId,
    source: Option<SourceId>,
    job: Option<JobId>,
    mode: &'static str,
    workspace: PathBuf,
    started: Instant,
}
fn directory(path: &Path) -> Result<()> {
    if !path.try_exists()? {
        fs::DirBuilder::new().mode(0o700).create(path)?;
    }
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.mode() & 0o077 != 0 || m.uid() != unsafe { libc::geteuid() } {
        return Err(invalid("private ingestion directory required"));
    }
    Ok(())
}
fn workspace(store: &Store, role: &str) -> Result<PathBuf> {
    let tmp = store.root().join("tmp");
    directory(&tmp)?;
    let path = tmp.join(role);
    directory(&path)?;
    Ok(path)
}
fn read(path: &Path) -> Result<Value> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.mode() & 0o077 != 0
        || meta.len() > 524288
    {
        return Err(invalid("invalid private ingestion worker response"));
    }
    let mut data = Vec::new();
    fs::File::open(path)?.take(524289).read_to_end(&mut data)?;
    if data.len() > 524288 {
        return Err(invalid("ingestion response too large"));
    }
    Ok(serde_json::from_slice(&data)?)
}
pub(crate) fn worker(store: &Store) -> Result<(PathBuf, PathBuf)> {
    let (python, exporter) = crate::installation::analytical_worker(store.root())?;
    let worker = exporter
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| invalid("invalid worker layout"))?
        .join("ingest/worker.py");
    if !worker.is_file() {
        return Err(invalid(
            "install the ingestion preview or configure the bundled Python runtime with python/analytics/export.py",
        ));
    }
    Ok((python, worker))
}
fn note(store: &Store, id: JobId, error: Option<&str>) -> Result<()> {
    let base = store.root().join("ingest");
    directory(&base)?;
    let dir = base.join("jobs");
    directory(&dir)?;
    let path = dir.join(format!("{id}.json"));
    let mut value = if path.exists() {
        read(&path)?
    } else {
        json!({})
    };
    value["error"] = json!(error);
    supervisor::write_json(&path, &value)
}
pub(crate) fn status(store: &Store, project: ProjectId, id: JobId) -> Result<Value> {
    let j = store.ingest_job(project, id)?;
    let mut value = json!(j);
    let path = store.root().join("ingest/jobs").join(format!("{id}.json"));
    if path.exists() {
        let evidence = read(&path)?;
        value["error"] = evidence["error"].clone();
        value["metrics"] = evidence["metrics"].clone();
    }
    value["source"] = json!(store.ingest_source(project, j.load.source_id)?);
    Ok(value)
}
pub(crate) fn source_status(store: &Store, project: ProjectId, id: SourceId) -> Result<Value> {
    let source = store.ingest_source(project, id)?;
    let dir = store.root().join("tmp").join(format!("ingest-source-{id}"));
    let mut inspection = Value::Null;
    let mut error = Value::Null;
    let mut progress = Value::Null;
    if source.generation == store.generation() {
        if dir.join("result.json").exists() {
            let report = read(&dir.join("result.json"))?;
            if source.state == "staged" {
                inspection = report["value"]["inspection"].clone();
            }
            error = report["error"].clone();
        }
        if dir.join("progress.json").exists() {
            progress = read(&dir.join("progress.json"))?;
        }
    }
    Ok(json!({"source":source,"inspection":inspection,"error":error,"progress":progress}))
}
impl Service {
    pub(crate) fn inspect(
        &mut self,
        store: &mut Store,
        binding: &Binding,
        path: PathBuf,
        delimiter: String,
        header: bool,
        null_strings: Vec<String>,
    ) -> Result<Value> {
        binding.validate(store)?;
        worker(store)?;
        if self
            .tasks
            .values()
            .any(|t| matches!(t.mode, "stage" | "uploaded" | "preview"))
        {
            return Err(conflict("a source inspection is already active"));
        }
        if !path.is_absolute() || path.as_os_str().len() > 4096 {
            return Err(invalid(
                "source path must be an explicit absolute regular file",
            ));
        }
        let options = Mapping {
            version: 1,
            format: Format::Csv,
            delimiter: delimiter.clone(),
            header,
            null_strings: null_strings.clone(),
            columns: vec![Column {
                input: "0".into(),
                name: "probe".into(),
                data_type: DataType::Text,
                nullable: true,
            }],
        };
        options.fingerprint()?;
        let name = path
            .file_name()
            .and_then(|p| p.to_str())
            .ok_or_else(|| invalid("source filename must be UTF-8"))?;
        let source = store.acquire_source(binding.project_id, name)?;
        let role = format!("ingest-source-{}", source.id);
        let part = store.source_path(source.id, "part")?;
        let input = json!({"path":path,"part":part,"options":{"delimiter":delimiter,"header":header,"null_strings":null_strings}});
        if let Err(error) = self.launch(
            store,
            &role,
            binding.project_id,
            Some(source.id),
            None,
            "stage",
            input,
        ) {
            store.abandon_source(binding.project_id, source.id)?;
            store.dispose_source(binding.project_id, source.id, false)?;
            return Err(error);
        }
        source_status(store, binding.project_id, source.id)
    }
    pub(crate) fn cancel_source(&mut self, store: &mut Store, id: SourceId) -> Result<()> {
        if let Some(task) = self.tasks.values_mut().find(|t| t.source == Some(id)) {
            task.started = Instant::now() - Duration::from_secs(601);
            self.tick(store, false)?;
        }
        Ok(())
    }
    pub(crate) fn inspect_uploaded(
        &mut self,
        store: &mut Store,
        binding: &Binding,
        id: SourceId,
        options: Mapping,
    ) -> Result<Value> {
        options.fingerprint()?;
        if self.tasks.values().any(|t| t.source.is_some()) {
            return Err(conflict("a source inspection is already active"));
        }
        let source = store.ingest_source(binding.project_id, id)?;
        if source.generation != store.generation()
            || source.expires_at_ms <= chrono::Utc::now().timestamp_millis()
        {
            return Err(conflict("source expired; upload again"));
        }
        let (mode, suffix) = match source.state.as_str() {
            "receiving" => ("uploaded", "part"),
            "staged" => ("preview", "source"),
            _ => return Err(conflict("source is unavailable")),
        };
        let path = store.source_path(id, suffix)?;
        self.launch(store, &format!("ingest-source-{id}"), binding.project_id, Some(id), None, mode,
            json!({"path":path,"options":{"delimiter":options.delimiter,"header":options.header,"null_strings":options.null_strings}}))?;
        source_status(store, binding.project_id, id)
    }
    fn launch(
        &mut self,
        store: &mut Store,
        role: &str,
        project: ProjectId,
        source: Option<SourceId>,
        job: Option<JobId>,
        mode: &'static str,
        mut input: Value,
    ) -> Result<()> {
        let (python, worker) = worker(store)?;
        let dir = workspace(store, role)?;
        for name in ["result.json", "progress.json"] {
            let _ = fs::remove_file(dir.join(name));
        }
        input["mode"] = json!(mode);
        input["root"] = json!(store.root());
        input["workspace"] = json!(dir);
        input["deadline_ms"] = json!(
            chrono::Utc::now().timestamp_millis()
                + if mode == "reconcile" { 20_000 } else { 600_000 }
        );
        supervisor::write_json(&dir.join("input.json"), &input)?;
        let branch = if mode == "load" {
            let j = store.ingest_job(project, job.unwrap())?;
            Some((j.load.branch_id, j.load.branch_revision))
        } else {
            None
        };
        let launch = Launch {
            root: store.root().into(),
            generation: store.generation(),
            role: role.into(),
            token: OperationId::new().to_string(),
            branch,
            argv: vec![
                python.to_string_lossy().into(),
                worker.to_string_lossy().into(),
                "--input".into(),
                dir.join("input.json").to_string_lossy().into(),
            ],
            env: BTreeMap::from([
                ("PATH".into(), "/usr/bin:/bin".into()),
                ("PYTHONNOUSERSITE".into(), "1".into()),
                ("PYTHONUNBUFFERED".into(), "1".into()),
                ("ARROW_DEFAULT_MEMORY_POOL".into(), "system".into()),
                ("OMP_NUM_THREADS".into(), "1".into()),
                ("OPENBLAS_NUM_THREADS".into(), "1".into()),
            ]),
            cwd: dir.clone(),
        };
        let child = supervisor::start_owned_before(
            store,
            &launch,
            &dir.join("launch.json"),
            &dir.join("worker.log"),
            |s, p| {
                if mode == "load" {
                    s.start_ingest(project, job.unwrap(), p)?;
                }
                Ok(())
            },
        )?;
        let process = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role)
            .ok_or_else(|| conflict("missing owned ingestion process"))?;
        self.tasks.insert(
            role.into(),
            Task {
                child,
                process,
                project,
                source,
                job,
                mode,
                workspace: dir,
                started: Instant::now(),
            },
        );
        Ok(())
    }
    fn input(store: &Store, j: &Job) -> Result<Value> {
        let b = store.branch(j.load.branch_id)?;
        if b.endpoint.desired_state != DesiredState::Running || b.observed_revision != b.revision {
            return Err(conflict("import awaits its running origin branch"));
        }
        let port = b
            .ports
            .ok_or_else(|| conflict("origin branch has no compute ports"))?
            .sql;
        // Owned imports hold the durable lifecycle protection. Receipt recovery
        // must remain possible after TTL expiry, without admitting new user work.
        Ok(
            json!({"job":j.id,"load":j.load,"origin":store.ingest_origin()?,"fingerprint":j.load.mapping.fingerprint()?,"receipt_ddl":RECEIPT_DDL,
            "source":store.root().join("ingest/sources").join(format!("{}.source",j.load.source_id)),
            "connection":{"host":"127.0.0.1","port":port,"user":"cloud_admin","password":store.endpoint_password(b.endpoint.id)?,"dbname":"postgres"}}),
        )
    }
    pub(crate) fn tick(&mut self, store: &mut Store, stopping: bool) -> Result<bool> {
        let roles: Vec<_> = self.tasks.keys().cloned().collect();
        for role in roles {
            let t = self.tasks.get_mut(&role).unwrap();
            let cancel = t
                .job
                .map(|id| store.ingest_job(t.project, id).map(|j| j.cancel_requested))
                .transpose()?
                .unwrap_or(false);
            let force = (stopping && t.mode != "reconcile")
                || (cancel && t.mode == "load")
                || t.started.elapsed()
                    > Duration::from_secs(if t.mode == "reconcile" { 22 } else { 600 });
            let exit_status = t.child.try_wait()?;
            let ended = exit_status.is_some();
            // A short COPY can finish between ticks. Consume its last bounded
            // sample before fencing. Missing/unreadable samples do not block it.
            if t.mode == "load"
                && let Ok(value) = read(&t.workspace.join("progress.json"))
            {
                if let (Some(parsed), Some(copied)) =
                    (value["parsed_rows"].as_u64(), value["copied_rows"].as_u64())
                {
                    let j = store.ingest_job(t.project, t.job.unwrap())?;
                    if j.state == State::Loading {
                        store.ingest_progress(t.project, j.id, &t.process, parsed, copied)?;
                    }
                }
            }
            if !ended && !force {
                continue;
            }
            supervisor::stop(&t.process)?;
            let _ = t.child.wait();
            store.forget_native_process(&t.process)?;
            let t = self.tasks.remove(&role).unwrap();
            let report = if t.workspace.join("result.json").exists() {
                read(&t.workspace.join("result.json"))
                    .unwrap_or_else(|_| json!({"ok":false,"error":"invalid_worker_response"}))
            } else {
                json!({"ok":false,"error":if force{"cancelled_or_limit"}else{"worker_lost"},"exit_code":exit_status.and_then(|s| s.code())})
            };
            if !t.workspace.join("result.json").exists() {
                supervisor::write_json(&t.workspace.join("result.json"), &report)?;
            }
            // Credentials and launch tickets are never retained as job history.
            for name in ["input.json", "launch.json", "worker.log"] {
                let _ = fs::remove_file(t.workspace.join(name));
            }
            if let Some(source) = t.source {
                let completed = (|| -> Result<()> {
                    if report["ok"] != true || force {
                        return Err(conflict("acquisition interrupted"));
                    }
                    let result = &report["value"];
                    let inspection: Inspection = serde_json::from_value(
                        json!({"version":1,"source_id":source,"source_sha256":result["sha256"],"mapping":result["inspection"]["mapping"],"rows":result["inspection"]["rows"],"sample_only":true}),
                    )?;
                    inspection.validate()?;
                    if t.mode == "preview" {
                        let current = store.ingest_source(t.project, source)?;
                        if current.sha256.as_deref() != result["sha256"].as_str() {
                            return Err(conflict("staged source changed"));
                        }
                        return Ok(());
                    }
                    store.seal_source_verified(
                        t.project,
                        source,
                        result["bytes"]
                            .as_u64()
                            .ok_or_else(|| invalid("missing staged size"))?,
                        result["sha256"]
                            .as_str()
                            .ok_or_else(|| invalid("missing staged hash"))?,
                    )?;
                    Ok(())
                })();
                if completed.is_err() {
                    store.abandon_source(t.project, source)?;
                    if report["ok"] == true {
                        supervisor::write_json(
                            &t.workspace.join("result.json"),
                            &json!({"ok":false,"error":"invalid_staging_evidence"}),
                        )?;
                    }
                }
            }

            if let Some(id) = t.job {
                if t.mode == "load" {
                    if stopping {
                        store.cancel_ingest(t.project, id)?;
                    }
                    store.fence_ingest(t.project, id)?;
                    if report["metrics"].is_object() {
                        note(store, id, None)?;
                        let path = store.root().join("ingest/jobs").join(format!("{id}.json"));
                        let mut evidence = read(&path)?;
                        evidence["metrics"] = report["metrics"].clone();
                        evidence["metrics"]["decoded_bytes"] =
                            report["value"]["decoded_bytes"].clone();
                        supervisor::write_json(&path, &evidence)?;
                    }

                    if report["ok"] == true && report["value"]["state"] == "rejected_before_load" {
                        store.reject_ingest_before_load(t.project, id)?;
                        note(store, id, Some("target_exists"))?;
                    }
                    if report["ok"] != true {
                        note(
                            store,
                            id,
                            Some(report["error"].as_str().unwrap_or("worker_lost")),
                        )?;
                    }
                } else {
                    if report["ok"] == true {
                        let outcome: Reconciliation =
                            serde_json::from_value(report["value"].clone())?;
                        match store.reconcile_ingest(t.project, id, outcome) {
                            Ok(j) => {
                                if j.state == State::Succeeded {
                                    note(store, id, None)?;
                                }
                            }
                            Err(_) => note(store, id, Some("receipt_or_target_conflict"))?,
                        }
                    } else {
                        note(
                            store,
                            id,
                            Some(report["error"].as_str().unwrap_or("receipt_unavailable")),
                        )?;
                    }
                    self.retry_at
                        .insert(id.to_string(), Instant::now() + Duration::from_secs(2));
                }
                fs::remove_dir_all(&t.workspace)?;
            }
        }
        for j in store.ingest_active()? {
            if self.tasks.values().any(|t| t.job == Some(j.id)) {
                continue;
            }
            if j.state == State::Loading {
                store.fence_ingest(j.load.project_id, j.id)?;
                continue;
            }
            if j.state == State::Queued && stopping {
                store.cancel_ingest(j.load.project_id, j.id)?;
                continue;
            }
            if self
                .retry_at
                .get(&j.id.to_string())
                .is_some_and(|at| *at > Instant::now())
            {
                continue;
            }
            let mode = if j.state == State::Queued {
                "load"
            } else {
                "reconcile"
            };
            let result = (|| {
                if mode == "load" {
                    store.connection(j.load.project_id, j.load.branch_id)?;
                }
                let input = Self::input(store, &j)?;
                let role = if mode == "load" {
                    format!("ingest-{}", j.id)
                } else {
                    format!("ingest-check-{}", j.id)
                };
                self.launch(
                    store,
                    &role,
                    j.load.project_id,
                    None,
                    Some(j.id),
                    mode,
                    input,
                )
            })();
            if result.is_err() {
                note(store, j.id, Some("origin_or_worker_unavailable"))?;
                if mode == "load" {
                    store.fail_queued_ingest(j.load.project_id, j.id)?;
                }
                self.retry_at
                    .insert(j.id.to_string(), Instant::now() + Duration::from_secs(2));
            }
        }
        let active = store.ingest_active()?;
        self.retry_at
            .retain(|id, _| active.iter().any(|job| job.id.to_string() == *id));
        store.cleanup_ingest()?;
        Ok(self.tasks.is_empty() && store.ingest_active()?.is_empty())
    }
}
