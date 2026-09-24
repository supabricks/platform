//! Owned bounded materialization workers; publisher alone exposes a completed group.
use super::*;
use crate::incremental::Run;
use sha2::{Digest, Sha256};
impl Cell {
    fn stop_incremental(&mut self, store: &mut Store, r: &Run) -> Result<()> {
        let role = format!("incremental-{}", r.id);
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
    pub(super) fn control_incremental(&mut self, store: &mut Store) -> Result<()> {
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
            if result.is_file() && result.metadata()?.len() <= 2 * 1024 * 1024 {
                let v: Value = serde_json::from_slice(&fs::read(&result)?)?;
                if v["id"] == json!(r.id)
                    && v["worker_generation"] == json!(store.generation())
                    && r.worker_generation == store.generation()
                {
                    self.stop_incremental(store, &r)?;
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
            let role = format!("incremental-{}", r.id);
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
            if live {
                if supervisor::os::rss(process.as_ref().unwrap().pid)? > 768 * 1024 * 1024 {
                    self.stop_incremental(store, &r)?;
                    store.fail_incremental(&mut r, "incremental_memory_budget", false)?;
                }
                continue;
            }
            if self.launches.contains_key(&role) || process.is_some() {
                if r.started_at_ms
                    .is_some_and(|at| chrono::Utc::now().timestamp_millis() - at < 3000)
                {
                    continue;
                }
                self.stop_incremental(store, &r)?;
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
            let config = json!({"id":r.id,"epoch_id":r.epoch_id,"ordinal":p.ordinal,"source_revision":r.source_revision,"identity":c.identity,"worker_generation":store.generation(),"workspace":work,"generation":self.root.join("analytics/incremental").join(r.storage_generation.unwrap_or(c.id).to_string()),"storage_generation":r.storage_generation,"previous_generation":previous.as_ref().map(|d|crate::analytics_v2::data_root(&self.root,d)).transpose()?,"spool":self.root.join("capture").join(c.id.to_string()).join("spool/spool.sqlite3"),"bootstrap_id":bootstrap,"bootstrap_manifest":self.root.join("analytics/staging").join(bootstrap.to_string()).join("manifest.json"),"bootstrap_lsn":c.bootstrap_lsn,"after_lsn":r.after_lsn,"target_lsn":r.target_lsn,"previous":previous,"deadline_ms":r.deadline_ms});
            write_json(&input, &config)?;
            if result.exists() {
                fs::remove_file(&result)?;
            }
            r.worker_generation = store.generation();
            r.state = "running".into();
            r.started_at_ms = Some(chrono::Utc::now().timestamp_millis());
            r.attempts += 1;
            store.save_incremental(&r)?;
            self.add(self.launch(
                &role,
                vec![path(&python)?, "-B".into(), path(&worker)?, path(&input)?],
                Some((r.branch_id, r.source_revision)),
                BTreeMap::new(),
                work,
            ))?;
            self.update()?;
        }
        Ok(())
    }
}
