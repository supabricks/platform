//! Owned bounded materialization workers; publisher alone exposes a completed group.
use super::*;
use crate::incremental::Run;
use sha2::{Digest, Sha256};
use std::time::{Duration, Instant};

/// Ephemeral ownership only. Recovery fences every process before rebuilding it.
pub(super) struct Worker {
    role: String,
    run: Run,
    scope: Value,
    busy: bool,
    born: Instant,
    idle: Instant,
    requests: u32,
}
impl Cell {
    fn stop_incremental(&mut self, store: &mut Store, r: &Run) -> Result<()> {
        if let Some(worker) = self.apply_workers.get(&r.capture_id) {
            if worker.run.id == r.id
                && (worker.busy || matches!(r.state.as_str(), "failed" | "cancelled"))
            {
                let role = worker.role.clone();
                self.apply_workers.remove(&r.capture_id);
                return self.stop_apply_role(store, &role);
            }
        }
        let role = format!("incremental-{}", r.id);
        // Completed runs must not stop a process that has been retained/reassigned.
        if self.apply_workers.values().any(|w| w.role == role) {
            return Ok(());
        }
        self.stop_apply_role(store, &role)
    }
    fn stop_apply_role(&mut self, store: &mut Store, role: &str) -> Result<()> {
        self.launches.remove(role);
        if let Some(p) = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role)
        {
            supervisor::stop(&p)?;
            store.forget_native_process(&p)?;
        }
        if self.processes.remove(role).is_some() {
            self.update()?;
        }
        let mailbox = self.root.join("analytics/apply-workers").join(role);
        if mailbox.exists() {
            fs::remove_dir_all(mailbox)?;
        }
        Ok(())
    }
    fn apply_scope(&self, store: &Store, r: &Run, c: &crate::capture::Capture) -> Result<Value> {
        let (python, exporter) = crate::installation::analytical_worker(store.root())?;
        Ok(
            json!({"python":python,"worker":exporter.with_file_name("incremental_worker.py"),"identity":c.identity,"worker_generation":store.generation(),
            "source_revision":r.source_revision,"storage_generation":r.storage_generation,"storage_profile":r.storage_profile,
            "policy_revision":store.sync_policy(c.project_id,c.policy_id)?.revision}),
        )
    }
    fn control_apply_workers(&mut self, store: &mut Store) -> Result<()> {
        let records = store.native_processes()?;
        let mut stop = Vec::new();
        for (id, worker) in &self.apply_workers {
            let authorized = (|| -> Result<bool> {
                let c = store.capture(worker.run.project_id, *id)?;
                store.capture_live(&c)?;
                let policy = store.sync_policy(c.project_id, c.policy_id)?;
                Ok(c.desired == "running"
                    && c.state != "resync_required"
                    && c.state != "deleted"
                    && policy.state == "active"
                    && store.branch(worker.run.branch_id)?.revision == worker.run.source_revision
                    && self.apply_scope(store, &worker.run, &c)? == worker.scope)
            })()
            .unwrap_or(false);
            let rss = records
                .iter()
                .find(|p| p.role == worker.role)
                .map(|p| supervisor::os::rss(p.pid))
                .transpose()?
                .unwrap_or(0);
            if !authorized
                || (!worker.busy
                    && (worker.born.elapsed() >= Duration::from_secs(60)
                        || worker.idle.elapsed() >= Duration::from_secs(5)
                        || worker.requests >= 64
                        || rss >= 512 * 1024 * 1024))
            {
                stop.push((*id, worker.role.clone()));
            } else if worker.busy && rss > 768 * 1024 * 1024 {
                let mut r = store.incremental_run(worker.run.project_id, worker.run.id)?;
                store.fail_incremental(&mut r, "incremental_memory_budget", false)?;
                stop.push((*id, worker.role.clone()));
            }
        }
        for (id, role) in stop {
            self.apply_workers.remove(&id);
            self.stop_apply_role(store, &role)?;
        }
        Ok(())
    }
    pub(super) fn control_incremental(&mut self, store: &mut Store) -> Result<()> {
        self.control_apply_workers(store)?;
        for mut r in store.incremental_runs()? {
            if matches!(r.state.as_str(), "requested" | "running" | "ready") {
                if store.incremental_live(&r).is_err()
                    || chrono::Utc::now().timestamp_millis() > r.deadline_ms
                {
                    self.stop_incremental(store, &r)?;
                    store.fail_incremental(&mut r, "materialization_fenced_or_expired", false)?;
                }
            } else {
                self.stop_incremental(store, &r)?;
                let work = self
                    .root
                    .join("analytics/apply-work")
                    .join(r.id.to_string());
                if work.exists() {
                    fs::remove_dir_all(&work)?;
                }
            }
        }
        // Compaction writes another root. Retained history, readers and catalog
        // publications keep the old root until every reference has drained.
        let roots = self.root.join("analytics/incremental");
        if roots.is_dir() {
            for entry in fs::read_dir(&roots)?.take(257) {
                let e = entry?;
                if let Some(id) = e.file_name().to_str().and_then(|n| {
                    n.strip_suffix(".initializing")
                        .unwrap_or(n)
                        .parse::<OperationId>()
                        .ok()
                }) && !store.incremental_root_referenced(id)?
                {
                    if !e.file_type()?.is_dir() {
                        return Err(invalid("unsafe incremental GC root"));
                    }
                    let Some(identity) = store.incremental_root_identity(id)? else {
                        continue;
                    };
                    let marker = e.path().join("owner.json");
                    if marker.is_symlink() || !marker.is_file() || marker.metadata()?.len() > 65536
                    {
                        continue;
                    }
                    let value: Value = serde_json::from_slice(&fs::read(marker)?)?;
                    if value["identity"] != identity
                        || value["storage_generation"]
                            .as_str()
                            .unwrap_or_else(|| identity["generation"].as_str().unwrap_or(""))
                            != id.to_string()
                    {
                        return Err(invalid("incremental GC ownership mismatch"));
                    }
                    fs::remove_dir_all(e.path())?;
                    fs::File::open(&roots)?.sync_all()?;
                }
            }
        }
        Ok(())
    }
    pub(super) fn tick_incremental(&mut self, store: &mut Store) -> Result<()> {
        let _profile = crate::sync_profile::span("apply.dispatch");
        for mut r in store.active_incremental()? {
            if r.state == "ready" {
                continue;
            }
            let c = store.incremental_live(&r)?;
            let work = self
                .root
                .join("analytics/apply-work")
                .join(r.id.to_string());
            let result = work.join("result.json");
            if r.state == "running"
                && result.is_file()
                && result.metadata()?.len() <= 2 * 1024 * 1024
            {
                let v: Value = serde_json::from_slice(&fs::read(&result)?)?;
                if v["id"] == json!(r.id)
                    && v["worker_generation"] == json!(store.generation())
                    && r.worker_generation == store.generation()
                {
                    let pooled = self
                        .apply_workers
                        .get(&r.capture_id)
                        .filter(|w| w.busy && w.run.id == r.id);
                    if let Some(worker) = pooled {
                        let live = store.native_processes()?.iter().any(|p| {
                            p.role == worker.role
                                && supervisor::os::identity(p.pid)
                                    .ok()
                                    .flatten()
                                    .is_some_and(|i| !i.zombie && i.start == p.start_identity)
                        });
                        let done = self
                            .root
                            .join("analytics/apply-workers")
                            .join(&worker.role)
                            .join("done.json");
                        let mut reusable = false;
                        let finished = if done.is_file() && done.metadata()?.len() <= 65536 {
                            let receipt: Value = serde_json::from_slice(&fs::read(done)?)?;
                            reusable = receipt["reusable"] == true;
                            receipt["id"] == json!(r.id)
                                && receipt["attempt"] == json!(r.attempts)
                                && receipt["worker_generation"] == json!(store.generation())
                        } else {
                            false
                        };
                        // No publication/GC while a live worker still owns request handles.
                        if live && !finished {
                            continue;
                        }
                        if live && finished && reusable && v["state"] == "ready" {
                            let worker = self.apply_workers.get_mut(&r.capture_id).unwrap();
                            worker.busy = false;
                            worker.idle = Instant::now();
                        } else {
                            self.stop_incremental(store, &r)?;
                        }
                    } else {
                        self.stop_incremental(store, &r)?;
                    }
                    if v["state"] == "deferred" {
                        if store
                            .defer_incremental(&mut r, &v, chrono::Utc::now().timestamp_millis())
                            .is_err()
                        {
                            store.fail_incremental(
                                &mut r,
                                "invalid_incremental_deferral",
                                false,
                            )?;
                        }
                        continue;
                    }
                    if store.record_journal_read(&mut r, &v).is_err() {
                        store.fail_incremental(&mut r, "invalid_journal_read_accounting", false)?;
                        continue;
                    }
                    if v["state"] == "failed" {
                        store.fail_incremental(
                            &mut r,
                            v["error"]
                                .as_str()
                                .filter(|s| {
                                    s.len() <= 80
                                        && s.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
                                })
                                .unwrap_or("incremental_worker_failed"),
                            false,
                        )?;
                        continue;
                    }
                    if v["state"] == "ready" {
                        let accepted = (|| -> Result<()> {
                            let mut d = v["descriptor"].clone();
                            if d["format_version"] != 2
                                || d["installation_id"] != c.identity["installation_id"]
                                || d["manifest"]["id"] != json!(r.id)
                                || d["ordinal"] != json!(store.publication(r.id)?.ordinal)
                                || d["export_id"] != json!(r.id)
                                || d["epoch_id"] != json!(r.epoch_id)
                                || d["source_revision"] != json!(r.source_revision)
                                || d["manifest"]["capture_identity"] != c.identity
                                || d["manifest"]["storage_generation"]
                                    != json!(r.storage_generation)
                                || crate::incremental::StorageProfile::from_manifest(
                                    &d["manifest"],
                                )? != r.storage_profile
                            {
                                return Err(invalid("worker epoch identity mismatch"));
                            }
                            let m = &d["manifest"];
                            for key in ["branch_id", "project_id", "tenant_id", "timeline_id"] {
                                if m["source"][key] != c.identity[key] {
                                    return Err(invalid("worker source identity mismatch"));
                                }
                            }
                            let baseline: Value = serde_json::from_slice(&fs::read(
                                self.root
                                    .join("analytics/staging")
                                    .join(
                                        c.bootstrap_id
                                            .ok_or_else(|| conflict("missing bootstrap"))?
                                            .to_string(),
                                    )
                                    .join("manifest.json"),
                            )?)?;
                            let tables = m["tables"]
                                .as_array()
                                .ok_or_else(|| invalid("missing epoch tables"))?;
                            let initial = baseline["tables"]
                                .as_array()
                                .ok_or_else(|| invalid("missing bootstrap tables"))?;
                            if tables.len() != initial.len() {
                                return Err(invalid("incomplete group map"));
                            }
                            for (t, b) in tables.iter().zip(initial) {
                                for field in ["oid", "schema", "name", "columns"] {
                                    if t[field] != b[field] {
                                        return Err(invalid("epoch schema changed"));
                                    }
                                }
                            }
                            crate::analytics_v2::layout(store.root(), &d)?;
                            let bytes = serde_json::to_vec(&d["manifest"])?;
                            d["manifest_sha256"] = json!(hex::encode(Sha256::digest(&bytes)));
                            let stage = self.root.join("analytics/staging").join(r.id.to_string());
                            dir(&stage)?;
                            write_private(&stage.join("manifest.json"), &bytes)?;
                            fs::File::open(&stage)?.sync_all()?;
                            fs::File::open(stage.parent().unwrap())?.sync_all()?;
                            store.incremental_ready(&mut r, &d)?;
                            Ok(())
                        })();
                        if accepted.is_err() {
                            store.fail_incremental(&mut r, "invalid_incremental_receipt", false)?;
                        }
                        continue;
                    }
                }
            }
            if store.pause_deferred_incremental(&mut r)? {
                continue;
            }
            if r.retry_at_ms
                .is_some_and(|at| chrono::Utc::now().timestamp_millis() < at)
            {
                continue;
            }
            let scope = self.apply_scope(store, &r, &c)?;
            if let Some(worker) = self.apply_workers.get(&r.capture_id) {
                if worker.scope != scope {
                    let role = worker.role.clone();
                    self.apply_workers.remove(&r.capture_id);
                    self.stop_apply_role(store, &role)?;
                } else if worker.busy && worker.run.id != r.id {
                    continue; // One in-flight request per capture, never a queue.
                }
            }
            let role = self
                .apply_workers
                .get(&r.capture_id)
                .map(|w| w.role.clone())
                .unwrap_or_else(|| format!("incremental-{}", r.id));
            let process = store
                .native_processes()?
                .into_iter()
                .find(|p| p.role == role);
            let live = process.as_ref().is_some_and(|p| {
                supervisor::os::identity(p.pid)
                    .ok()
                    .flatten()
                    .is_some_and(|i| !i.zombie && i.start == p.start_identity)
            });
            let reuse = live
                && self
                    .apply_workers
                    .get(&r.capture_id)
                    .is_some_and(|w| !w.busy);
            if live && !reuse {
                if supervisor::os::rss(process.as_ref().unwrap().pid)? > 768 * 1024 * 1024 {
                    self.stop_incremental(store, &r)?;
                    store.fail_incremental(&mut r, "incremental_memory_budget", false)?;
                }
                continue;
            }
            if !reuse && (self.launches.contains_key(&role) || process.is_some()) {
                if r.started_at_ms
                    .is_some_and(|at| chrono::Utc::now().timestamp_millis() - at < 3000)
                {
                    continue;
                }
                self.apply_workers.remove(&r.capture_id);
                self.stop_apply_role(store, &role)?;
            }
            if r.attempts >= 3 {
                store.fail_incremental(&mut r, "incremental_worker_retries_exhausted", false)?;
                continue;
            }
            let (python, exporter) = crate::installation::analytical_worker(store.root())?;
            let worker = exporter.with_file_name("incremental_worker.py");
            if !worker.is_file() {
                store.fail_incremental(&mut r, "incremental_worker_not_installed", false)?;
                continue;
            }
            dir(&self.root.join("analytics/apply-work"))?;
            dir(&work)?;
            dir(&self.root.join("analytics/incremental"))?;
            fs::File::open(self.root.join("analytics"))?.sync_all()?;
            let input = work.join("input.json");
            let previous = r
                .previous_epoch
                .map(|id| {
                    store
                        .snapshot(r.project_id, id)
                        .map(|s| s.publication.descriptor)
                })
                .transpose()?
                .flatten();
            let p = store.publication(r.id)?;
            let bootstrap = c
                .bootstrap_id
                .ok_or_else(|| conflict("missing capture bootstrap"))?;
            let mut config = json!({"id":r.id,"attempt":r.attempts+1,"epoch_id":r.epoch_id,"ordinal":p.ordinal,"source_revision":r.source_revision,"identity":c.identity,"worker_generation":store.generation(),"workspace":work,"generation":self.root.join("analytics/incremental").join(r.storage_generation.unwrap_or(c.id).to_string()),"storage_generation":r.storage_generation,"previous_generation":previous.as_ref().map(|d|crate::analytics_v2::data_root(&self.root,d)).transpose()?,"spool":self.root.join("capture").join(c.id.to_string()).join("spool/spool.sqlite3"),"bootstrap_id":bootstrap,"bootstrap_manifest":self.root.join("analytics/staging").join(bootstrap.to_string()).join("manifest.json"),"bootstrap_lsn":c.bootstrap_lsn,"after_lsn":r.after_lsn,"target_lsn":r.target_lsn,"previous":previous,"deadline_ms":r.deadline_ms});
            config["storage_profile"] = json!(r.storage_profile);
            // Issued only after incremental_live validates the run and authority.
            // Capture compares the exact request with this private input.json,
            // and rechecks generation/source/policy before its completion marker.
            config["journal_access"] = self.journal_access(store, &c, r.source_revision)?;
            config["reuse_authority"] = scope.clone();
            config["reuse_worker"] = json!(reuse || self.apply_workers.len() < 4);
            write_json(&input, &config)?;
            if result.exists() {
                fs::remove_file(&result)?;
            }
            r.worker_generation = store.generation();
            r.state = "running".into();
            r.retry_at_ms = None;
            r.error = None;
            r.started_at_ms = Some(chrono::Utc::now().timestamp_millis());
            r.attempts += 1;
            store.save_incremental(&r)?;
            if reuse {
                // incremental_live above revalidates the current run/epoch/authority.
                let slot = self.apply_workers.get_mut(&r.capture_id).unwrap();
                write_json(
                    &self
                        .root
                        .join("analytics/apply-workers")
                        .join(&slot.role)
                        .join("input.json"),
                    &config,
                )?;
                slot.run = r.clone();
                slot.busy = true;
                slot.requests += 1;
                continue;
            }
            let mut argv = vec![path(&python)?, "-B".into(), path(&worker)?, path(&input)?];
            if self.apply_workers.len() < 4 {
                let mailbox = self.root.join("analytics/apply-workers").join(&role);
                dir(&mailbox)?;
                write_json(&mailbox.join("input.json"), &config)?;
                argv[3] = path(&mailbox.join("input.json"))?;
                self.apply_workers.insert(
                    r.capture_id,
                    Worker {
                        role: role.clone(),
                        run: r.clone(),
                        scope,
                        busy: true,
                        born: Instant::now(),
                        idle: Instant::now(),
                        requests: 1,
                    },
                );
            }
            self.add(self.launch(
                &role,
                argv,
                Some((r.branch_id, r.source_revision)),
                BTreeMap::new(),
                self.root.clone(),
            ))?;
            self.update()?;
        }
        Ok(())
    }
}
