//! One read-only decode slot. Durable apply/publication still own all mutations.
use super::*;
use crate::incremental::{Run, lsn};
use std::time::Instant;

const MIB: u64 = 1024 * 1024;

pub(super) struct Preparation {
    origin: Run,
    authorization: Value,
    role: String,
    work: PathBuf,
    child: Child,
    born: Instant,
}

fn bounded_json(path: &Path, limit: u64) -> Option<Value> {
    let m = fs::symlink_metadata(path).ok()?;
    if !m.is_file() || m.len() > limit {
        return None;
    }
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

fn receipt_in_range(authorization: &Value, receipt: &Value) -> bool {
    let valid = || -> Option<bool> {
        let end = lsn(receipt["end_lsn"].as_str()?).ok()?;
        Some(
            end > lsn(authorization["after_lsn"].as_str()?).ok()?
                && end <= lsn(authorization["target_lsn"].as_str()?).ok()?
                && receipt["rows"].as_u64()? <= 65536
                && (1..=65536).contains(&receipt["transactions"].as_u64()?)
                && receipt["bytes"].as_u64()? <= 64 * MIB
                && receipt["input_bytes"].as_u64()? <= 16 * MIB
                && receipt["peak_rss_bytes"].as_u64()? <= 256 * MIB,
        )
    };
    valid().unwrap_or(false)
}

fn range(
    after: &str,
    decoded: &str,
    current_target: &str,
    captured: &str,
    barrier: Option<&str>,
) -> Result<Option<String>> {
    let end = lsn(decoded)?;
    if end <= lsn(after)? || end > lsn(current_target)? {
        return Ok(None);
    }
    let target = if let Some(barrier) = barrier {
        if lsn(barrier)? < lsn(captured)? {
            barrier
        } else {
            captured
        }
    } else {
        captured
    };
    Ok((lsn(target)? > end).then(|| target.to_owned()))
}

impl Cell {
    fn preparation_live(&self, store: &Store, slot: &Preparation) -> Result<bool> {
        let r = store.incremental_run(slot.origin.project_id, slot.origin.id)?;
        let c = store.capture(r.project_id, r.capture_id)?;
        store.capture_live(&c)?;
        let p = store.sync_policy(c.project_id, c.policy_id)?;
        let parent_live = r
            .sync_run_id
            .map(|id| {
                store
                    .sync_run(r.project_id, id)
                    .map(|p| matches!(p.state.as_str(), "running" | "succeeded"))
            })
            .transpose()?
            .unwrap_or(true);
        Ok(
            matches!(r.state.as_str(), "running" | "ready" | "succeeded")
                && r.attempts == slot.origin.attempts
                && parent_live
                && c.desired == "running"
                && c.state == "capturing"
                && p.state == "active"
                && !p.pause_requested
                && store.branch(r.branch_id)?.revision == r.source_revision
                && json!(store.generation()) == slot.authorization["worker_generation"]
                && c.identity == slot.authorization["identity"]
                && self.journal_access(store, &c, r.source_revision)?
                    == slot.authorization["journal_access"]
                && chrono::Utc::now().timestamp_millis() < r.deadline_ms,
        )
    }

    pub(super) fn discard_preparation(&mut self, store: &mut Store) -> Result<()> {
        if let Some(slot) = &mut self.preparation {
            if let Some(p) = store
                .native_processes()?
                .into_iter()
                .find(|p| p.role == slot.role)
            {
                supervisor::stop(&p)?;
                store.forget_native_process(&p)?;
            }
            let _ = slot.child.wait()?;
            if slot.work.exists() {
                fs::remove_dir_all(&slot.work)?;
            }
        }
        self.preparation = None;
        Ok(())
    }

    pub(super) fn control_preparation(&mut self, store: &mut Store) -> Result<()> {
        let Some(slot) = &self.preparation else {
            return Ok(());
        };
        let records = store.native_processes()?;
        let mut rss = 0;
        let mut preparation_rss = 0;
        for p in records
            .iter()
            .filter(|p| p.role.starts_with("incremental-"))
        {
            let used = supervisor::os::rss(p.pid)?;
            rss += used;
            if p.role == slot.role {
                preparation_rss = used;
            }
        }
        let live = self.preparation_live(store, slot).unwrap_or(false);
        let failed = self
            .preparation
            .as_mut()
            .unwrap()
            .child
            .try_wait()?
            .is_some_and(|s| !s.success());
        if !live || failed || rss > 768 * MIB || preparation_rss > 256 * MIB {
            self.discard_preparation(store)?;
        }
        Ok(())
    }

    pub(super) fn start_preparation(
        &mut self,
        store: &mut Store,
        r: &Run,
        c: &crate::capture::Capture,
    ) -> Result<()> {
        if self.preparation.is_some()
            || r.state != "running"
            || r.storage_profile.is_compact()
            || r.previous_epoch.is_none()
            || self.preparation_attempt == Some((r.id, r.attempts))
        {
            return Ok(());
        }
        let hint_path = self
            .root
            .join("analytics/apply-work")
            .join(r.id.to_string())
            .join("decoded.json");
        let Some(hint) = bounded_json(&hint_path, 65536) else {
            return Ok(());
        };
        if hint["id"] != json!(r.id)
            || hint["attempt"] != json!(r.attempts)
            || hint["worker_generation"] != json!(store.generation())
        {
            return Ok(());
        }
        // A previously reported cursor often exposes only a small fraction of
        // the next batch. Wait for one fresh capture observation before freezing
        // the optional range. Apply/publication never waits for this stage.
        let Some(decoded_at) = hint["decoded_at_ms"].as_i64() else {
            return Ok(());
        };
        if c.observed_at_ms.is_none_or(|at| at < decoded_at) {
            return Ok(());
        }
        self.preparation_attempt = Some((r.id, r.attempts));
        let Some(end) = hint["end_lsn"].as_str() else {
            return Ok(());
        };
        let Some(schema) = hint["schema_sha256"]
            .as_str()
            .filter(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
        else {
            return Ok(());
        };
        let parent = r
            .sync_run_id
            .map(|id| store.sync_run(r.project_id, id))
            .transpose()?;
        let barrier = parent
            .as_ref()
            .filter(|p| !p.config.continuous())
            .and_then(|p| p.target_lsn.as_deref());
        if parent
            .as_ref()
            .is_some_and(|p| !p.config.continuous() && barrier.is_none())
        {
            return Ok(());
        }
        let Some(captured) = &c.captured_lsn else {
            return Ok(());
        };
        let Some(target) = range(&r.after_lsn, end, &r.target_lsn, captured, barrier)? else {
            return Ok(());
        };
        let mut rss = 0;
        for p in store
            .native_processes()?
            .iter()
            .filter(|p| p.role.starts_with("incremental-"))
        {
            rss += supervisor::os::rss(p.pid)?;
        }
        if rss > 512 * MIB {
            return Ok(());
        }
        let (python, exporter) = crate::installation::analytical_worker(store.root())?;
        let worker = exporter.with_file_name("prepare_worker.py");
        if !worker.is_file() {
            return Ok(());
        }
        let id = OperationId::new();
        let work = self
            .root
            .join("analytics/prepare-work")
            .join(id.to_string());
        dir(work.parent().unwrap())?;
        dir(&work)?;
        let authorization = json!({"preparation":1,"id":id,"attempt":1,"epoch_id":r.epoch_id,
            "identity":c.identity,"worker_generation":store.generation(),"source_revision":r.source_revision,
            "bootstrap_lsn":c.bootstrap_lsn,"after_lsn":end,"target_lsn":target,"deadline_ms":r.deadline_ms,
            "journal_access":self.journal_access(store,c,r.source_revision)?,"storage_profile":r.storage_profile,
            "workspace":work,"schema_sha256":schema,"decoded_at_ms":decoded_at,"capture_observed_at_ms":c.observed_at_ms});
        write_json(&work.join("input.json"), &authorization)?;
        // One fixed role/file/log avoids unbounded launch metadata. start_owned
        // records OS identity before opening the execution gate, including when
        // the daemon dies during launch. Recovery fences it before deleting data.
        let role = "incremental-prepare".to_owned();
        let launch = self.launch(
            &role,
            vec![
                path(&python)?,
                "-B".into(),
                path(&worker)?,
                path(&work.join("input.json"))?,
            ],
            Some((r.branch_id, r.source_revision)),
            BTreeMap::new(),
            self.root.clone(),
        );
        let log = self.root.join("logs/incremental-prepare.log");
        if log.exists() {
            fs::remove_file(&log)?;
        }
        let child = match supervisor::start_owned(
            store,
            &launch,
            &self.root.join("launches/incremental-prepare.json"),
            &log,
        ) {
            Ok(child) => child,
            Err(error) => {
                // start_owned has already fenced a failed launch. Retire its
                // unused grant so successive failures cannot accumulate files.
                fs::remove_dir_all(&work)?;
                return Err(error);
            }
        };
        self.preparation = Some(Preparation {
            origin: r.clone(),
            authorization,
            role,
            work,
            child,
            born: Instant::now(),
        });
        Ok(())
    }

    pub(super) fn attach_preparation(
        &mut self,
        store: &mut Store,
        r: &Run,
        config: &mut Value,
    ) -> Result<()> {
        let Some(slot) = &self.preparation else {
            return Ok(());
        };
        // The current run cannot consume its own speculative successor.
        if slot.origin.id == r.id {
            return Ok(());
        }
        let live = self.preparation_live(store, slot).unwrap_or(false);
        let predecessor = store.incremental_run(slot.origin.project_id, slot.origin.id)?;
        let compatible = live
            && predecessor.state == "succeeded"
            && r.previous_epoch == Some(slot.origin.epoch_id)
            && r.capture_id == slot.origin.capture_id
            && json!(r.after_lsn) == slot.authorization["after_lsn"]
            && lsn(&r.target_lsn)? >= lsn(slot.authorization["target_lsn"].as_str().unwrap_or(""))?
            && r.storage_profile == slot.origin.storage_profile;
        let done = self
            .preparation
            .as_mut()
            .unwrap()
            .child
            .try_wait()?
            .is_some_and(|s| s.success());
        if compatible && done {
            let slot = self.preparation.as_ref().unwrap();
            if let Some(receipt) = bounded_json(&slot.work.join("result.json"), 65536)
                && receipt["state"] == "ready"
                && receipt["id"] == slot.authorization["id"]
                && receipt_in_range(&slot.authorization, &receipt)
            {
                let destination =
                    PathBuf::from(config["workspace"].as_str().unwrap()).join("prepared");
                if destination.exists() {
                    fs::remove_dir_all(&destination)?;
                }
                fs::rename(&slot.work, &destination)?;
                config["prepared_batch"] = json!({"authorization":slot.authorization,"receipt":receipt,
                    "age_ms":slot.born.elapsed().as_millis()});
                config["prepared_read_after"] = receipt["end_lsn"].clone();
            }
        }
        // Never wait on a late preparer. Ordinary authorized journal reading is
        // the fallback, before any initialization or mutation.
        self.discard_preparation(store)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn suffix_authority_requires_a_bounded_complete_preparation_receipt() {
        let authorization = json!({"after_lsn":"0/20","target_lsn":"0/50"});
        let receipt = json!({"end_lsn":"0/40","rows":65536,"transactions":65536,"bytes":64*MIB,"input_bytes":16*MIB,"peak_rss_bytes":256*MIB});
        assert!(receipt_in_range(&authorization, &receipt));
        for (key, value) in [
            ("end_lsn", json!("0/20")),
            ("end_lsn", json!("0/51")),
            ("end_lsn", json!("invalid")),
            ("rows", json!(65537)),
            ("transactions", json!(65537)),
            ("transactions", json!(0)),
            ("bytes", json!(64 * MIB + 1)),
            ("input_bytes", json!(16 * MIB + 1)),
            ("peak_rss_bytes", json!(256 * MIB + 1)),
        ] {
            let mut changed = receipt.clone();
            changed[key] = value;
            assert!(!receipt_in_range(&authorization, &changed));
        }
    }
    #[test]
    fn complete_end_and_triggered_barrier_bound_the_fixed_range() {
        assert_eq!(
            range("0/10", "0/20", "0/30", "0/50", None).unwrap(),
            Some("0/50".into())
        );
        assert_eq!(
            range("0/10", "0/20", "0/30", "0/50", Some("0/30")).unwrap(),
            Some("0/30".into())
        );
        for end in ["0/10", "0/31"] {
            assert!(range("0/10", end, "0/30", "0/50", None).unwrap().is_none());
        }
        assert!(
            range("0/10", "0/30", "0/30", "0/50", Some("0/30"))
                .unwrap()
                .is_none()
        );
        assert!(
            range("0/10", "0/20", "0/30", "0/20", None)
                .unwrap()
                .is_none()
        );
    }
}
