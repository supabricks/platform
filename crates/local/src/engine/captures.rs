//! Native owned capture worker and hidden A01 bootstrap orchestration.
use super::*;
use crate::capture::Capture;
use std::io::Read;

impl Cell {
    fn stop_capture(&mut self, store: &mut Store, c: &Capture) -> Result<()> {
        let role = format!("capture-{}", c.id);
        self.launches.remove(&role);
        if let Some(p) = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role)
        {
            supervisor::stop(&p)?;
            store.forget_native_process(&p)?;
        }
        if self.processes.remove(&role).is_some() {
            self.update()?;
        }
        Ok(())
    }
    pub(super) fn control_captures(&mut self, store: &mut Store) -> Result<()> {
        for mut c in store.captures()? {
            if matches!(c.desired.as_str(), "running" | "paused") && store.capture_live(&c).is_err()
            {
                self.stop_capture(store, &c)?;
                c.desired = "fenced".into();
                c.state = "resync_required".into();
                c.error = Some("source_or_policy_changed".into());
                c.cleanup_complete = false;
                store.save_capture(&c)?;
            }
        }
        Ok(())
    }
    pub(super) fn tick_captures(&mut self, store: &mut Store) -> Result<()> {
        let _profile = crate::sync_profile::span("capture.dispatch");
        for mut c in store.captures()? {
            let root = self.root.join("capture").join(c.id.to_string());
            let status_path = root.join("status.json");
            let role = format!("capture-{}", c.id);
            let now = chrono::Utc::now().timestamp_millis();
            if status_path.is_file() && status_path.metadata()?.len() <= 256 * 1024 {
                let mut bytes = Vec::new();
                fs::File::open(&status_path)?
                    .take(256 * 1024 + 1)
                    .read_to_end(&mut bytes)?;
                if let Ok(v) = serde_json::from_slice::<Value>(&bytes)
                    && v["identity"] == c.identity
                    && v["worker_generation"] == json!(c.worker_generation)
                    && c.worker_generation == store.generation()
                {
                    let stamp = v["observed_at_ms"].as_i64();
                    if stamp != c.observed_at_ms {
                        c.observed_at_ms = stamp;
                        if let Some(value) = v["start_lsn"].as_str() {
                            c.start_lsn = Some(value.to_owned());
                        }
                        if let Some(value) = v["captured_lsn"].as_str() {
                            c.captured_lsn = Some(value.to_owned());
                        }
                        if let Some(value) = v["source_lsn"].as_str() {
                            c.source_lsn = Some(value.to_owned());
                        }
                        if let Some(value) = v["retained_wal_bytes"].as_u64() {
                            c.retained_wal_bytes = Some(value);
                        }
                        if let Some(value) = v["spool_bytes"].as_u64() {
                            c.spool_bytes = Some(value);
                        }
                        c.bootstrap_lsn = v["bootstrap_lsn"].as_str().map(str::to_owned);
                        c.barrier = v.get("barrier").filter(|v| !v.is_null()).cloned();
                        c.progress = v.get("progress").filter(|v| !v.is_null()).cloned();
                        match v["state"].as_str() {
                            Some("deleted") => {
                                c.cleanup_complete = true;
                                self.stop_capture(store, &c)?;
                            }
                            Some("resync_required") => {
                                c.desired = "fenced".into();
                                c.state = "resync_required".into();
                                c.error = Some(
                                    v["error"]
                                        .as_str()
                                        .unwrap_or("capture_failed")
                                        .chars()
                                        .take(128)
                                        .collect(),
                                );
                                self.stop_capture(store, &c)?;
                            }
                            Some("unavailable") => {
                                c.state = "unavailable".into();
                                c.error = Some(
                                    match v["error"].as_str() {
                                        Some("spool_backpressure") => "spool_backpressure",
                                        Some("spool_migration_busy") => "spool_migration_busy",
                                        Some("spool_migration_budget") => "spool_migration_budget",
                                        _ => "source_unavailable",
                                    }
                                    .into(),
                                );
                            }
                            Some("established" | "capturing" | "paused")
                                if matches!(c.desired.as_str(), "running" | "paused") =>
                            {
                                c.error = (v["error"].as_str() == Some("spool_backpressure"))
                                    .then(|| "spool_backpressure".into());
                                c.state = if c.desired == "paused" {
                                    "paused"
                                } else if c.bootstrap_lsn.is_some() {
                                    "capturing"
                                } else {
                                    "bootstrapping"
                                }
                                .into();
                            }
                            _ => (),
                        }
                        store.save_capture(&c)?;
                    }
                }
            }
            let branch = store.branch(c.branch_id)?;
            if branch.endpoint.desired_state == DesiredState::Deleted
                && branch.observed_revision == branch.revision
            {
                self.stop_capture(store, &c)?;
                c.cleanup_complete = true;
            }
            if c.cleanup_complete && matches!(c.desired.as_str(), "deleted" | "fenced") {
                if let Some(id) = c.bootstrap_id {
                    let export = store.export(id)?;
                    if !matches!(export.state.as_str(), "complete" | "failed" | "cancelled") {
                        store.cancel_export(c.project_id, id)?;
                        continue;
                    }
                }
                store.capture_lease(&c, false)?;
                if c.desired == "deleted" {
                    self.stop_capture(store, &c)?;
                    // No active worker and no source slot remain. Remove only this UUID workspace.
                    if root.exists() {
                        fs::remove_dir_all(&root)?;
                        fs::File::open(root.parent().unwrap())?.sync_all()?;
                    }
                    store.finish_capture_delete(&mut c)?;
                } else {
                    store.save_capture(&c)?;
                }
                continue;
            }
            if branch.endpoint.desired_state != DesiredState::Running
                || !self.connection_ready(store, &branch)?
            {
                continue;
            }
            store.capture_lease(&c, true)?;
            // Repair the bootstrap admission/link crash gap by the durable internal key.
            if c.bootstrap_id.is_none() {
                c.bootstrap_id = store.capture_bootstrap_for_key(&c)?;
                store.save_capture(&c)?;
            }
            if c.start_lsn.is_some()
                && c.bootstrap_id.is_none()
                && c.desired == "running"
                && store.active_exports()?.is_empty()
            {
                let policy = store.sync_policy(c.project_id, c.policy_id)?;
                let binding = crate::api::Binding {
                    project_id: c.project_id,
                    worktree: self.root.clone(),
                };
                let v = crate::api::handle(
                    store,
                    Some(self),
                    &binding,
                    crate::api::Action::Export {
                        branch: c.branch_id.to_string(),
                        key: format!("internal:capture-bootstrap:{}", c.id),
                        limits: policy.config.limits,
                    },
                )?;
                c.bootstrap_id = Some(serde_json::from_value(v["id"].clone())?);
                store.save_capture(&c)?;
            }
            let bootstrap = if let Some(id) = c.bootstrap_id {
                let e = store.export(id)?;
                if matches!(e.state.as_str(), "failed" | "cancelled")
                    && matches!(c.desired.as_str(), "running" | "paused")
                {
                    c.desired = "fenced".into();
                    c.state = "resync_required".into();
                    c.error = Some("bootstrap_failed".into());
                    store.save_capture(&c)?;
                    self.stop_capture(store, &c)?;
                }
                if e.state == "complete" {
                    Some(
                        json!({"id":id,"manifest":self.root.join("analytics/staging").join(id.to_string()).join("manifest.json")}),
                    )
                } else {
                    None
                }
            } else {
                None
            };
            let (python, exporter) = crate::installation::analytical_worker(store.root())?;
            let worker = exporter.with_file_name("capture_worker.py");
            if !worker.is_file() {
                return Err(invalid("capture worker is not installed beside export.py"));
            }
            dir(&self.root.join("capture"))?;
            fs::File::open(&self.root)?.sync_all()?;
            dir(&root)?;
            fs::File::open(root.parent().unwrap())?.sync_all()?;
            let input = root.join("control.json");
            let config = json!({"identity":c.identity,"worker_generation":store.generation(),"desired":if c.desired=="fenced"{"deleted"}else{c.desired.as_str()},
                "socket_dir":self.root.join("tmp").join(branch.endpoint.id.to_string()),"port":branch.ports.ok_or_else(||conflict("source has no native ports"))?.sql,
                "report_interval_ms":if store.sync_policy(c.project_id,c.policy_id)?.config.continuous(){250}else{1000},"barrier_request":store.triggered_barrier_request(c.id)?,"published_lsn":store.capture_published_lsn(c.id)?,"spool_bytes":c.limits.spool_bytes,"wal_bytes":c.limits.wal_bytes,"bootstrap":bootstrap});
            if !input.is_file()
                || serde_json::from_slice::<Value>(&fs::read(&input)?)
                    .ok()
                    .as_ref()
                    != Some(&config)
            {
                write_json(&input, &config)?;
            }
            let existing = store
                .native_processes()?
                .into_iter()
                .find(|p| p.role == role);
            let live = existing.as_ref().is_some_and(|p| {
                supervisor::os::identity(p.pid)
                    .ok()
                    .flatten()
                    .is_some_and(|i| !i.zombie && i.start == p.start_identity)
            });
            if !live && (self.launches.contains_key(&role) || existing.is_some()) {
                // Allow the gated child time to register; retry real exits/outages with backoff.
                if c.observed_at_ms.is_none_or(|at| now - at < 3000) {
                    continue;
                }
                self.stop_capture(store, &c)?;
            }
            if !self.launches.contains_key(&role) {
                if c.identity.get("service_authority").is_some()
                    && matches!(c.desired.as_str(), "running" | "paused")
                    && crate::governed::postgres::inspect(crate::governed::postgres::Target {
                        capture_identity: Some(c.identity.clone()),
                        port: branch
                            .ports
                            .ok_or_else(|| conflict("source has no native ports"))?
                            .sql,
                        password: store.endpoint_password(branch.endpoint.id)?,
                    })
                    .is_err()
                {
                    // Refuse broad source credentials before the first worker can
                    // create replication resources or invoke source DDL triggers.
                    // Generation zero proves that no owned worker has run yet.
                    c.cleanup_complete = c.worker_generation == 0;
                    c.desired = "fenced".into();
                    c.state = "resync_required".into();
                    c.error = Some("unsupported_governed_source_profile".into());
                    store.save_capture(&c)?;
                    continue;
                }
                if status_path.exists() {
                    fs::remove_file(&status_path)?;
                }
                c.worker_generation = store.generation();
                c.progress = None;
                c.observed_at_ms = Some(now);
                store.save_capture(&c)?;
                self.add(self.launch(
                    &role,
                    vec![path(&python)?, "-B".into(), path(&worker)?, path(&input)?],
                    Some((branch.branch.id, branch.revision)),
                    BTreeMap::new(),
                    root.clone(),
                ))?;
                self.update()?;
            }
        }
        Ok(())
    }
}
