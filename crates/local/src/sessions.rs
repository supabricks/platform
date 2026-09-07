//! On-demand Sail workers. No process may read a generation before its durable
//! session reference and native PID/birth identity have committed.
use crate::{
    api::{Action, Binding},
    store::{
        AnalyticalSession, Error, Result, Store,
        error::{conflict, invalid},
    },
    supervisor::{self, Launch},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::Child,
};
use supabricks_core::{
    error::OperationError,
    resource::{EpochId, OperationId, ProjectId},
};

#[derive(Default)]
pub struct Sessions {
    children: BTreeMap<String, Child>,
    pub last_error: Option<String>,
}
fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
fn role(id: OperationId) -> String {
    format!("analytics-session-{id}")
}
fn workspace(store: &Store, id: OperationId) -> PathBuf {
    store.root().join("session-work").join(id.to_string())
}
fn bounded_json(path: &Path) -> Result<Value> {
    if !fs::symlink_metadata(path)?.is_file() {
        return Err(invalid("worker response is not a regular file"));
    }
    let mut data = Vec::new();
    fs::File::open(path)?.take(524289).read_to_end(&mut data)?;
    if data.len() > 524288 {
        return Err(invalid("worker response exceeds 512 KiB"));
    }
    Ok(serde_json::from_slice(&data)?)
}
pub fn worker_config(store: &Store) -> Result<(PathBuf, PathBuf)> {
    let config: Value =
        serde_json::from_slice(&fs::read(store.root().join("analytics.json")).map_err(|_| {
            invalid("run analytics configure with the locked Python environment first")
        })?)?;
    let python = PathBuf::from(
        config["python"]
            .as_str()
            .ok_or_else(|| invalid("invalid analytical Python path"))?,
    );
    let worker = PathBuf::from(
        config["worker"]
            .as_str()
            .ok_or_else(|| invalid("invalid exporter path"))?,
    )
    .with_file_name("session.py");
    if !python.is_absolute() || !python.is_file() || !worker.is_absolute() || !worker.is_file() {
        return Err(invalid(
            "session.py must be installed beside the configured export.py",
        ));
    }
    Ok((python, worker))
}
impl Sessions {
    pub fn recover(store: &mut Store) -> Result<Self> {
        let mut this = Self::default();
        for p in store
            .native_processes()?
            .into_iter()
            .filter(|p| p.role.starts_with("analytics-session-"))
        {
            if p.root != store.root() {
                return Err(conflict(
                    "analytical worker belongs to a different data root",
                ));
            }
            supervisor::stop(&p)?;
            store.forget_native_process(&p)?;
        }
        for mut s in store.active_analytical_sessions()? {
            // Waiting refreshes are durable and may resume; launched endpoints
            // never survive a daemon generation or get silently rebound.
            if s.state != "waiting" {
                this.finish(store, &mut s, "daemon_restarted")?;
            }
        }
        Ok(this)
    }
    pub fn refresh(
        store: &mut Store,
        cell: Option<&crate::engine::Cell>,
        binding: &Binding,
        branch: String,
        key: String,
        limits: crate::store::ExportLimits,
    ) -> Result<Value> {
        let result = crate::api::handle(
            store,
            cell,
            binding,
            Action::Export {
                branch,
                key,
                limits,
            },
        )?;
        let id: OperationId = serde_json::from_value(result["id"].clone())?;
        store.mark_refresh(id)?;
        store.refresh_status(binding.project_id, id)
    }
    pub fn open(
        store: &mut Store,
        cell: Option<&crate::engine::Cell>,
        binding: &Binding,
        branch: Option<String>,
        epoch: Option<EpochId>,
        key: String,
        ttl_ms: u64,
    ) -> Result<Value> {
        let request = json!({"branch":branch,"epoch":epoch,"ttl_ms":ttl_ms});
        if let Some(s) = store.session_for_key(binding.project_id, &key, &request)? {
            return Ok(json!(s));
        }
        if !(10_000..=3_600_000).contains(&ttl_ms) {
            return Err(invalid("session lifetime requires 10–3600 seconds"));
        }
        worker_config(store)?;
        if store.active_analytical_sessions()?.len() >= 2 {
            return Err(conflict("both analytical session slots are occupied"));
        }
        let branch_id = if let Some(id) = epoch.filter(|_| branch.is_none()) {
            store
                .snapshot(binding.project_id, id)?
                .publication
                .branch_id
        } else {
            crate::api::resolve(store, binding, branch.as_deref())?
        };
        let epoch = match epoch {
            Some(e) => Some(e),
            None => match store.current_snapshot(binding.project_id, branch_id) {
                Ok(s) => Some(s.publication.epoch_id),
                Err(Error::Operation(OperationError::NotFound(_))) => None,
                Err(e) => return Err(e),
            },
        };
        let refresh = if epoch.is_none() {
            let existing = store.pending_refreshes()?.into_iter().find(|(p, id)| {
                *p == binding.project_id
                    && store.export(*id).is_ok_and(|e| e.source_id == branch_id)
            });
            Some(match existing {
                Some((_, id)) => id,
                None => serde_json::from_value(
                    Self::refresh(
                        store,
                        cell,
                        binding,
                        branch_id.to_string(),
                        format!("session:{}", hex::encode(Sha256::digest(key.as_bytes()))),
                        Default::default(),
                    )?["id"]
                        .clone(),
                )?,
            })
        } else {
            None
        };
        Ok(json!(store.admit_analytical_session(
            binding.project_id,
            branch_id,
            &key,
            request,
            epoch,
            refresh,
            ttl_ms
        )?))
    }
    pub fn cancel_refresh(store: &mut Store, project: ProjectId, id: OperationId) -> Result<Value> {
        let e = store.export_in_project(project, id)?;
        if e.state == "complete" {
            store.discard_export(project, id)?;
        } else if !matches!(e.state.as_str(), "failed" | "cancelled") {
            store.cancel_export(project, id)?;
        }
        store.fail_refresh(id, "cancelled")?;
        store.refresh_status(project, id)
    }
    pub fn query(
        store: &mut Store,
        project: ProjectId,
        id: OperationId,
        sql: String,
        max_rows: usize,
        max_bytes: usize,
        timeout_ms: u64,
    ) -> Result<Value> {
        if sql.is_empty()
            || sql.len() > 32768
            || !(1..=1000).contains(&max_rows)
            || !(1024..=262144).contains(&max_bytes)
            || !(100..=30000).contains(&timeout_ms)
        {
            return Err(invalid(
                "query requires SQL <=32 KiB, 1–1000 rows, 1–256 KiB result, 100–30000 ms",
            ));
        }
        let mut s = store.analytical_session(project, id)?;
        if s.state != "ready" || now() >= s.expires_at_ms {
            return Err(conflict("analytical session is not ready or has expired"));
        }
        if s.query.as_ref().is_some_and(|q| q["state"] == "running") {
            return Err(conflict("session already has a running SQL request"));
        }
        let q = json!({"id":OperationId::new(),"session_id":s.id,"epoch_id":s.epoch_id,"state":"running","sql":sql,"max_rows":max_rows,"max_bytes":max_bytes,"deadline_ms":now()+timeout_ms as i64});
        s.query = Some(q.clone());
        store.save_analytical_session(&s)?;
        // A crash between journal and handoff fails the session on recovery;
        // a request is never automatically replayed.
        supervisor::write_json(&workspace(store, id).join("query.json"), &q)?;
        Ok(q)
    }
    pub fn query_status(
        store: &Store,
        project: ProjectId,
        id: OperationId,
        query: OperationId,
    ) -> Result<Value> {
        let s = store.analytical_session(project, id)?;
        s.query
            .filter(|q| q["id"] == json!(query))
            .ok_or_else(|| invalid("query result is absent or replaced by the next request"))
    }
    fn finish(&mut self, store: &mut Store, s: &mut AnalyticalSession, reason: &str) -> Result<()> {
        s.state = "closing".into();
        s.error = Some(reason.into());
        s.endpoint = None;
        store.save_analytical_session(s)?;
        if let Some(p) = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role(s.id))
        {
            supervisor::stop(&p)?;
            store.forget_native_process(&p)?;
        }
        if let Some(mut child) = self.children.remove(&s.id.to_string()) {
            let _ = child.wait()?;
        }
        // Removal follows proof of death; the durable reference stays until
        // cleanup completes, including after a filesystem error.
        let dir = workspace(store, s.id);
        if dir.exists() {
            fs::remove_dir_all(dir)?;
        }
        s.state = if matches!(reason, "closed" | "cancelled" | "expired") {
            "closed"
        } else {
            "failed"
        }
        .into();
        s.error = Some(reason.into());
        if let Some(q) = s.query.as_mut() {
            if q["state"] == "running" {
                q["state"] = json!("failed");
                q["error"] = json!(reason);
            }
        }
        store.save_analytical_session(s)
    }
    pub fn stop(&mut self, store: &mut Store) -> Result<()> {
        for mut s in store.active_analytical_sessions()? {
            self.finish(store, &mut s, "closed")?;
        }
        Ok(())
    }
    pub fn tick(&mut self, store: &mut Store) -> Result<()> {
        for (project, id) in store.pending_refreshes()? {
            if store.export(id)?.state == "complete" {
                if let Err(e) = store.publish_export(project, id) {
                    store.fail_refresh(id, &e.to_string())?;
                }
            }
        }
        for mut s in store.active_analytical_sessions()? {
            if s.state == "closing" {
                let reason = s.error.clone().unwrap_or_else(|| "closed".into());
                self.finish(store, &mut s, &reason)?;
                continue;
            }
            if now() >= s.expires_at_ms {
                self.finish(store, &mut s, "expired")?;
                continue;
            }
            if s.state == "waiting" {
                let status = store.refresh_status(s.project_id, s.refresh_id.unwrap())?;
                if status["state"] == "published" {
                    let epoch = serde_json::from_value(status["publication"]["epoch_id"].clone())?;
                    if let Err(e) = store.bind_session_epoch(&mut s, epoch) {
                        self.finish(store, &mut s, &e.to_string())?;
                    }
                } else if matches!(status["state"].as_str(), Some("failed" | "cancelled")) {
                    self.finish(store, &mut s, "initial_refresh_failed")?;
                }
                continue;
            }
            let result = self.tick_worker(store, &mut s);
            if let Err(e) = result {
                self.finish(store, &mut s, &e.to_string())?;
            }
        }
        Ok(())
    }
    fn tick_worker(&mut self, store: &mut Store, s: &mut AnalyticalSession) -> Result<()> {
        let dir = workspace(store, s.id);
        let process = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role(s.id));
        if process.is_none() {
            if s.state != "starting" {
                return Err(conflict("analytical_worker_disappeared"));
            }
            let (python, worker) = worker_config(store)?;
            let snapshot = store.snapshot(s.project_id, s.epoch_id.unwrap())?;
            if snapshot.state != "available" {
                return Err(conflict("snapshot became unavailable"));
            }
            fs::create_dir_all(&dir)?;
            let descriptor = snapshot
                .publication
                .descriptor
                .ok_or_else(|| invalid("missing snapshot descriptor"))?;
            let metadata = json!({"installation_id":descriptor["installation_id"],"project_id":s.project_id,"branch_id":s.branch_id,"epoch_id":s.epoch_id,"ordinal":snapshot.publication.ordinal,"source":descriptor["manifest"]["source"],"observed_at_ms":descriptor["manifest"]["observed_at_ms"],"published_at_ms":snapshot.publication.published_at_ms,"session_id":s.id,"expires_at_ms":s.expires_at_ms,"worker_started_at_ms":now()});
            supervisor::write_json(
                &dir.join("input.json"),
                &json!({"root":store.root(),"workspace":dir,"descriptor":descriptor,"metadata":metadata,"expires_at_ms":s.expires_at_ms}),
            )?;
            let env = BTreeMap::from([
                ("PATH".into(), "/usr/bin:/bin".into()),
                ("PYTHONNOUSERSITE".into(), "1".into()),
                ("PYSPARK_PYTHON".into(), python.to_string_lossy().into()),
                ("PYTHONUNBUFFERED".into(), "1".into()),
                ("OTEL_SDK_DISABLED".into(), "true".into()),
            ]);
            let launch = Launch {
                root: store.root().into(),
                generation: store.generation(),
                role: role(s.id),
                token: OperationId::new().to_string(),
                branch: None,
                argv: vec![
                    python.to_string_lossy().into(),
                    worker.to_string_lossy().into(),
                    "--input".into(),
                    dir.join("input.json").to_string_lossy().into(),
                ],
                env,
                cwd: dir.clone(),
            };
            let child = supervisor::start_owned(
                store,
                &launch,
                &dir.join("launch.json"),
                &dir.join("worker.log"),
            )?;
            self.children.insert(s.id.to_string(), child);
            s.metadata = Some(metadata);
            store.save_analytical_session(s)?;
            return Ok(());
        }
        if let Some(child) = self.children.get_mut(&s.id.to_string()) {
            let _ = child.try_wait()?;
        }
        if supervisor::members(&process.unwrap())?.is_empty() {
            let failure = dir.join("failure.json");
            let detail = if failure.exists() {
                bounded_json(&failure)?["error"]
                    .as_str()
                    .unwrap_or("worker failed")
                    .to_owned()
            } else {
                "worker exited without a report".into()
            };
            return Err(conflict(format!("analytical_worker_exit: {detail}")));
        }
        if s.state == "starting" {
            if dir.join("ready.json").exists() {
                let ready = bounded_json(&dir.join("ready.json"))?;
                if ready["session_id"] != json!(s.id) {
                    return Err(invalid("worker session identity mismatch"));
                }
                let port = ready["port"]
                    .as_u64()
                    .filter(|p| *p > 0 && *p <= 65535)
                    .ok_or_else(|| invalid("invalid Spark Connect port"))?;
                s.endpoint = Some(format!(
                    "sc://127.0.0.1:{port}/;user_id=supabricks;session_id={}",
                    s.id
                ));
                s.state = "ready".into();
                store.save_analytical_session(s)?;
            } else if now()
                - s.metadata
                    .as_ref()
                    .and_then(|m| m["worker_started_at_ms"].as_i64())
                    .unwrap_or(s.created_at_ms)
                > 120_000
            {
                return Err(conflict("analytical_startup_deadline"));
            }
        }
        if let Some(q) = s.query.clone().filter(|q| q["state"] == "running") {
            if now() >= q["deadline_ms"].as_i64().unwrap_or(0) {
                return Err(conflict("query_deadline; session closed"));
            }
            let result = dir.join("result.json");
            if result.exists() {
                let report = bounded_json(&result)?;
                if report["id"] == q["id"] {
                    if !matches!(report["state"].as_str(), Some("complete" | "failed"))
                        || report["epoch_id"] != json!(s.epoch_id)
                    {
                        return Err(invalid("invalid query report"));
                    }
                    s.query = Some(report);
                    store.save_analytical_session(s)?;
                }
            }
            if s.query.as_ref().is_some_and(|q| {
                q["state"] == "running" && now() >= q["deadline_ms"].as_i64().unwrap_or(0)
            }) {
                return Err(conflict("query_deadline; session closed"));
            }
        }
        Ok(())
    }
}
