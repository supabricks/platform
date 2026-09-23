//! Fixed source barriers and bounded SY03 batches owned by managed policy runs.
use super::*;
use crate::{
    capture::Capture,
    incremental::{Command as ApplyCommand, Run as Apply, lsn},
};
impl Store {
    pub(super) fn enroll_triggered(&mut self, p: &Policy) -> Result<()> {
        self.capture_command(
            p.project_id,
            crate::capture::Command::Start {
                policy_id: p.id,
                expected_revision: p.revision,
                key: format!("internal:triggered-enroll:{}:{}", p.id, p.revision),
                limits: Default::default(),
            },
        )?;
        Ok(())
    }
    pub(super) fn triggered_capture(&self, p: &Policy) -> Result<Capture> {
        let c = self
            .captures()?
            .into_iter()
            .find(|c| c.policy_id == p.id)
            .ok_or_else(|| conflict("triggered policy requires capture enrollment"))?;
        self.capture_live(&c)?;
        if c.identity["decoder_version"] != 2
            || c.desired != "running"
            || matches!(c.state.as_str(), "resync_required" | "deleting" | "deleted")
        {
            return Err(conflict(
                "triggered capture requires explicit resync or resume",
            ));
        }
        Ok(c)
    }
    fn triggered_run_capture(&self, p: &Policy, r: &Run) -> Result<Capture> {
        let c = self.triggered_capture(p)?;
        if r.capture_id != Some(c.id) {
            return Err(conflict("triggered run capture generation changed"));
        }
        Ok(c)
    }
    pub(super) fn delete_policy_capture(&self, p: &Policy) -> Result<()> {
        for mut c in self.captures()?.into_iter().filter(|c| c.policy_id == p.id) {
            c.desired = "deleted".into();
            c.state = "deleting".into();
            c.cleanup_complete = false;
            self.save_capture(&c)?;
        }
        Ok(())
    }
    pub(super) fn cancel_triggered_apply(&self, r: &Run, reason: &str) -> Result<()> {
        if let Some(id) = r.apply_id {
            let mut a = self.incremental_run(r.project_id, id)?;
            if matches!(a.state.as_str(), "requested" | "running" | "ready") {
                self.fail_incremental(&mut a, reason, true)?;
            }
        }
        Ok(())
    }
    pub(crate) fn triggered_publication_live(&self, a: &Apply) -> Result<()> {
        if let Some(id) = a.sync_run_id {
            let r = self.sync_run(a.project_id, id)?;
            let p = self.sync_policy(r.project_id, r.policy_id)?;
            if !r.config.incremental()
                || p.state != "active"
                || p.revision != r.policy_revision
                || r.state != "running"
                || r.capture_id != Some(a.capture_id)
                || r.apply_id != Some(a.id)
                || r.deadline_ms
                    .is_none_or(|at| super::super::now_ms().unwrap_or(i64::MAX) > at)
            {
                return Err(conflict("triggered run or policy is fenced"));
            }
            self.sync_source(&p)?;
            if r.target_lsn
                .as_ref()
                .is_none_or(|target| lsn(&a.target_lsn).ok() > lsn(target).ok())
            {
                return Err(conflict("batch exceeds triggered target"));
            }
        }
        Ok(())
    }
    pub(crate) fn triggered_barrier_request(
        &self,
        capture: OperationId,
    ) -> Result<Option<OperationId>> {
        if let Some(id) = self
            .active_sync_runs()?
            .into_iter()
            .find(|r| {
                r.config.mode == "triggered"
                    && r.capture_id == Some(capture)
                    && r.target_lsn.is_none()
            })
            .map(|r| r.id)
        {
            return Ok(Some(id));
        }
        let c = self.captures()?.into_iter().find(|c| c.id == capture);
        if let Some(c) = c {
            let p = self.sync_policy(c.project_id, c.policy_id)?;
            if p.config.continuous() && p.state == "active" && !p.pause_requested {
                return Ok(p
                    .observation
                    .and_then(|v| v["id"].as_str().and_then(|s| s.parse().ok())));
            }
        }
        Ok(None)
    }

    fn complete_triggered(&self, r: &mut Run, now: i64) -> Result<()> {
        r.state = "succeeded".into();
        r.finished_at_ms = Some(now);
        r.error = None;
        let mut p = self.sync_policy(r.project_id, r.policy_id)?;
        p.last_success_at_ms = Some(now);
        p.last_epoch_id = r.epoch_id.clone();
        p.error = None;
        if p.pause_requested {
            p.state = "paused".into();
            p.pause_requested = false;
        }
        self.db.execute_batch("SAVEPOINT triggered_success")?;
        let result = (|| {
            self.save_sync_run(r)?;
            self.save_sync_policy(&p)
        })();
        match result {
            Ok(()) => self.db.execute_batch("RELEASE triggered_success")?,
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO triggered_success; RELEASE triggered_success")?;
                return Err(e);
            }
        }
        Ok(())
    }
    pub(super) fn reconcile_triggered(&mut self, now: i64) -> Result<()> {
        for mut r in self
            .active_sync_runs()?
            .into_iter()
            .filter(|r| r.config.incremental())
        {
            // The publication transaction wins over a late cancellation/deadline.
            if let Some(id) = r.apply_id {
                let a = self.incremental_run(r.project_id, id)?;
                if a.state == "succeeded" {
                    r.source_lsn = a.applied_lsn;
                    r.epoch_id = Some(a.epoch_id.to_string());
                    r.published_artifact_id = Some(a.id);
                    r.apply_id = None;
                    if r.source_lsn == r.target_lsn {
                        self.complete_triggered(&mut r, now)?;
                        continue;
                    }
                    self.save_sync_run(&r)?;
                } else if matches!(a.state.as_str(), "failed" | "cancelled") {
                    self.fail_sync_run(r.id, "incremental_batch_failed_requires_resync", now)?;
                    continue;
                }
            }
            let mut p = self.sync_policy(r.project_id, r.policy_id)?;
            if p.pause_requested && r.apply_id.is_none() {
                self.cancel_sync_run(r, "continuous_paused_at_boundary", now)?;
                p.state = "paused".into();
                p.pause_requested = false;
                self.save_sync_policy(&p)?;
                continue;
            }
            if p.state != "active"
                || p.revision != r.policy_revision
                || self.triggered_run_capture(&p, &r).is_err()
                || r.deadline_ms.is_none_or(|at| now > at)
            {
                self.cancel_triggered_apply(&r, "triggered_run_fenced_or_expired")?;
                self.fail_sync_run(r.id, "triggered_run_fenced_or_expired", now)?;
            }
        }
        Ok(())
    }
    pub(crate) fn tick_triggered(&mut self, now: i64) -> Result<()> {
        for mut r in self
            .active_sync_runs()?
            .into_iter()
            .filter(|r| r.config.incremental())
        {
            if r.apply_id.is_some() {
                continue;
            }
            let p = self.sync_policy(r.project_id, r.policy_id)?;
            let c = self.triggered_run_capture(&p, &r)?;
            if c.state != "capturing"
                || c.bootstrap_lsn.is_none()
                || c.observed_at_ms.is_none_or(|at| now - at > 5000)
            {
                continue;
            }
            if r.target_lsn.is_none() && r.config.continuous() {
                r.target_lsn = c.captured_lsn.clone();
                self.save_sync_run(&r)?;
            }
            if r.target_lsn.is_none() {
                let Some(ref barrier) = c.barrier else {
                    continue;
                };
                if barrier["run_id"] != json!(r.id) {
                    continue;
                }
                let target = barrier["end_lsn"]
                    .as_str()
                    .ok_or_else(|| invalid("missing barrier boundary"))?;
                if lsn(target)? < lsn(c.bootstrap_lsn.as_ref().unwrap())?
                    || lsn(target)?
                        > lsn(c
                            .captured_lsn
                            .as_deref()
                            .ok_or_else(|| invalid("missing capture cursor"))?)?
                {
                    return Err(conflict("barrier outside verified captured prefix"));
                }
                r.target_lsn = Some(target.into());
                self.save_sync_run(&r)?;
            }
            if !self.active_incremental()?.is_empty() {
                continue;
            }
            if r.batches >= 64 {
                self.fail_sync_run(r.id, "triggered_batch_budget", now)?;
                continue;
            }
            self.db.execute_batch("SAVEPOINT triggered_batch")?;
            let result = (|| {
                let value = self.incremental_command_at(
                    r.project_id,
                    ApplyCommand::Apply {
                        capture_id: c.id,
                        key: format!("internal:triggered:{}:{}", r.id, r.batches),
                    },
                    r.target_lsn.as_deref(),
                    Some(r.id),
                )?;
                let mut a: Apply = serde_json::from_value(value)?;
                a.deadline_ms = a.deadline_ms.min(r.deadline_ms.unwrap());
                self.save_incremental(&a)?;
                r.apply_id = Some(a.id);
                r.batches += 1;
                r.state = "running".into();
                self.save_sync_run(&r)
            })();
            match result {
                Ok(()) => self.db.execute_batch("RELEASE triggered_batch")?,
                Err(_) => {
                    self.db
                        .execute_batch("ROLLBACK TO triggered_batch; RELEASE triggered_batch")?;
                    self.fail_sync_run(r.id, "triggered_batch_admission_failed", now)?;
                }
            }
        }
        Ok(())
    }
}

// On a stopped backup, preserve the target publication even if the daemon died
// before reconciling its parent run. Intermediate prefixes are not run success.
pub(super) fn restore_success(db: &rusqlite::Connection) -> Result<()> {
    let rows = db
        .prepare("SELECT record FROM sync_runs WHERE state IN ('queued','starting','running')")?
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for text in rows {
        let mut r: Run = serde_json::from_str(&text)?;
        if !r.config.incremental() {
            continue;
        }
        let Some(id) = r.apply_id else { continue };
        let published:Option<(String,i64,String)>=db.query_row("SELECT epoch_id,published_at_ms,descriptor FROM publications WHERE export_id=?1 AND state='published'",[id.to_string()],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?))).optional()?;
        if let Some((epoch, at, text)) = published {
            let d: Value = serde_json::from_str(&text)?;
            r.source_lsn = d["manifest"]["source"]["lsn"].as_str().map(str::to_owned);
            if r.source_lsn != r.target_lsn {
                continue;
            }
            r.state = "succeeded".into();
            r.finished_at_ms = Some(at);
            r.epoch_id = Some(epoch.clone());
            r.published_artifact_id = Some(id);
            r.apply_id = None;
            r.error = None;
            db.execute(
                "UPDATE sync_runs SET state='succeeded',record=?2 WHERE id=?1",
                params![r.id.to_string(), serde_json::to_string(&r)?],
            )?;
            db.execute("UPDATE sync_policies SET record=json_set(record,'$.last_success_at_ms',?2,'$.last_epoch_id',?3) WHERE id=?1",params![r.policy_id.to_string(),at,epoch])?;
        }
    }
    Ok(())
}
