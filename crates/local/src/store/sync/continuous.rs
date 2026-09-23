//! Continuous admission and observation; the same bounded publisher owns every cut.
use super::*;
use crate::incremental::lsn;

impl Store {
    pub(crate) fn capture_published_lsn(&self, id: OperationId) -> Result<Option<String>> {
        Ok(self
            .db
            .query_row(
                "SELECT published_lsn FROM incremental_heads WHERE capture_id=?1",
                [id.to_string()],
                |r| r.get(0),
            )
            .optional()?)
    }
    pub(crate) fn schedule_continuous(&mut self, now: i64) -> Result<()> {
        for mut p in self.sync_policies()?.into_iter().filter(|p| {
            p.config.continuous() && p.state == "active" && !p.pause_requested && p.error.is_none()
        }) {
            if p.observation.as_ref().is_none_or(|v| {
                v["requested_at_ms"]
                    .as_i64()
                    .is_none_or(|at| now - at >= 1000)
            }) {
                p.observation = Some(json!({"id":OperationId::new(),"requested_at_ms":now}));
                self.save_sync_policy(&p)?;
            }
            let last_admission: Option<i64> = self.db.query_row("SELECT json_extract(record,'$.admitted_at_ms') FROM sync_runs WHERE policy_id=?1 ORDER BY ordinal DESC LIMIT 1",[p.id.to_string()],|r|r.get(0)).optional()?;
            if self.active_sync_runs()?.iter().any(|r| r.policy_id == p.id)
                || last_admission.is_some_and(|at| {
                    now - at < p.config.continuous_config().batch_interval_ms as i64
                })
            {
                continue;
            }
            let Ok(c) = self.triggered_capture(&p) else {
                continue;
            };
            if c.state != "capturing"
                || c.bootstrap_lsn.is_none()
                || c.observed_at_ms
                    .is_none_or(|at| now - at > 5000 || at > now)
                || self.branch(c.branch_id)?.endpoint.desired_state != DesiredState::Running
            {
                continue;
            }
            if c.captured_lsn
                .as_ref()
                .is_none_or(|at| lsn(at).ok() < c.bootstrap_lsn.as_ref().and_then(|b| lsn(b).ok()))
            {
                continue;
            }
            let published = self.capture_published_lsn(c.id)?;
            if let (Some(old), Some(new)) = (&published, &c.captured_lsn) {
                if lsn(new)? < lsn(old)? {
                    p.error = Some("continuous_capture_cursor_regressed".into());
                    self.save_sync_policy(&p)?;
                    continue;
                }
                let data = c
                    .progress
                    .as_ref()
                    .and_then(|v| v["last_data_lsn"].as_str());
                if data.is_none_or(|at| lsn(at).ok().is_none_or(|n| n <= lsn(old).unwrap_or(0))) {
                    continue;
                }
            }
            // No timer backlog: a single persisted parent owns the sampled prefix.
            if self.admit_sync_run(&p, "continuous", None, now).is_err() {
                p.error = Some("continuous_admission_failed".into());
                self.save_sync_policy(&p)?;
            }
        }
        Ok(())
    }
    pub(super) fn sync_policy_view(&self, p: &Policy, now: i64) -> Result<Value> {
        let mut value = json!(p);
        if p.service_authority.is_some() && self.sync_authority_live(p).is_err() {
            value["authority_status"] = json!("revoked_or_unavailable");
            if p.state != "deleted" {
                value["state"] = json!("blocked");
                value["error"] =
                    json!("service_authority_changed; review grants and recreate the policy");
            }
        }
        let capture = p
            .capture_id
            .and_then(|id| self.capture(p.project_id, id).ok());
        value["capture_status"] = capture
            .as_ref()
            .map(|c| {
                json!({
                    "id":c.id,"state":c.state,"desired":c.desired,"error":c.error,
                    "observed_at_ms":c.observed_at_ms,"captured_lsn":c.captured_lsn,
                    "published_lsn":self.capture_published_lsn(c.id).ok().flatten(),
                    "source_lsn":c.source_lsn,"spool_bytes":c.spool_bytes,
                    "retained_wal_bytes":c.retained_wal_bytes,"cleanup_complete":c.cleanup_complete
                })
            })
            .unwrap_or(Value::Null);
        if !p.config.continuous() {
            return Ok(value);
        }
        let c = p
            .capture_id
            .and_then(|id| self.capture(p.project_id, id).ok());
        let published = c
            .as_ref()
            .map(|c| self.capture_published_lsn(c.id))
            .transpose()?
            .flatten();
        let progress = c.as_ref().and_then(|c| c.progress.as_ref());
        let observed = c.as_ref().and_then(|c| c.observed_at_ms);
        let fresh = c
            .as_ref()
            .is_some_and(|c| c.worker_generation == self.generation())
            && observed.is_some_and(|at| (0..=5000).contains(&(now - at)));
        let metrics_match = progress.is_some_and(|v| v["published_lsn"] == json!(published));
        let stream = progress.and_then(|v| v["stream_observed_at_ms"].as_i64());
        let stream_fresh = stream.is_some_and(|at| (0..=5000).contains(&(now - at)));
        let pending = if fresh && metrics_match {
            progress.and_then(|v| v["backlog_bytes"].as_u64())
        } else {
            None
        };
        let oldest = if fresh && metrics_match {
            progress.and_then(|v| v["oldest_commit_at_ms"].as_i64())
        } else {
            None
        };
        let barrier_time = progress.and_then(|v| v["barrier_commit_at_ms"].as_i64());
        let proof_ttl = p.config.continuous_config().freshness_ms.min(5000) as i64;
        let proved = barrier_time.is_some_and(|at| (0..=proof_ttl).contains(&(now - at)));
        let caught_up = published.as_ref().is_some_and(|at| {
            progress
                .and_then(|v| v["last_data_lsn"].as_str())
                .is_some_and(|last| {
                    lsn(last)
                        .ok()
                        .zip(lsn(at).ok())
                        .is_some_and(|(last, at)| last <= at)
                })
        }) && proved;
        let lag = if fresh
            && stream_fresh
            && c.as_ref().is_some_and(|c| c.state == "capturing")
            && self.branch(p.branch_id)?.endpoint.desired_state == DesiredState::Running
        {
            if caught_up {
                Some(0)
            } else {
                oldest.filter(|at| *at <= now).map(|at| now - at)
            }
        } else {
            None
        };
        let pressure = c.as_ref().is_some_and(|c| {
            c.spool_bytes
                .is_some_and(|n| n >= c.limits.spool_bytes * 4 / 5)
                || c.retained_wal_bytes
                    .is_some_and(|n| n >= c.limits.wal_bytes * 4 / 5)
        });
        let head_changed = if let Some(c) = &c {
            self.db.prepare("SELECT 1 FROM incremental_heads i LEFT JOIN snapshot_heads h ON h.branch_id=?2 WHERE i.capture_id=?1 AND (h.epoch_id IS NULL OR h.epoch_id!=i.epoch_id)")?.exists(params![c.id.to_string(),p.branch_id.to_string()])?
        } else {
            false
        };
        let state = if p.state == "deleted" {
            "deleted"
        } else if p.pause_requested {
            "pausing"
        } else if p.state == "paused" {
            "paused"
        } else if head_changed
            || p.state == "blocked"
            || c.as_ref().is_none_or(|c| {
                c.desired != "running"
                    || matches!(c.state.as_str(), "resync_required" | "deleted" | "deleting")
            })
        {
            "blocked"
        } else if p.error.is_some() {
            "failed"
        } else if c.as_ref().is_some_and(|c| c.state == "unavailable")
            || !fresh
            || self.branch(p.branch_id)?.endpoint.desired_state != DesiredState::Running
        {
            "unavailable"
        } else if published.is_none() || p.last_epoch_id.is_none() {
            "initializing"
        } else if !stream_fresh {
            "unavailable"
        } else if pressure
            || lag.is_some_and(|n| n > p.config.continuous_config().freshness_ms as i64)
        {
            "lagging"
        } else if caught_up {
            "healthy"
        } else {
            "catching_up"
        };
        value["continuous_status"] = json!({"state":state,"observed_at_ms":observed,
            "stream_observed_at_ms":stream,"source_barrier_at_ms":barrier_time,"published_lsn":published,
            "captured_lsn":c.as_ref().and_then(|c|c.captured_lsn.as_ref()),
            "source_lsn":c.as_ref().and_then(|c|c.source_lsn.as_ref()),
            "oldest_unpublished_commit_age_ms":lag,"lag_observed_at_ms":if lag.is_some(){Some(now)}else{None},
            "backlog_bytes":pending,"spool_bytes":c.as_ref().and_then(|c|c.spool_bytes),
            "retained_wal_bytes":c.as_ref().and_then(|c|c.retained_wal_bytes),"pressure":pressure,
            "capture_keeps_compute_awake":c.as_ref().is_some_and(|c| matches!(c.desired.as_str(),"running"|"paused")),
            "head_changed":head_changed,"error":p.error.as_ref().or_else(||c.as_ref().and_then(|c|c.error.as_ref()))});
        Ok(value)
    }
}
