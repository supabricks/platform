use super::{
    Result, Store,
    error::{conflict, invalid, missing},
    now_ms, parse,
};
use crate::{
    capture::Capture,
    incremental::{Command, Run, lsn},
};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use supabricks_core::resource::{EpochId, OperationId, ProjectId};
impl Store {
    pub fn incremental_run(&self, project: ProjectId, id: OperationId) -> Result<Run> {
        let text:String=self.db.query_row("SELECT r.record FROM incremental_runs r JOIN analytical_artifacts a ON a.id=r.id WHERE r.id=?1 AND a.project_id=?2",params![id.to_string(),project.to_string()],|r|r.get(0)).optional()?.ok_or_else(||missing("incremental run in project"))?;
        Ok(serde_json::from_str(&text)?)
    }
    pub(crate) fn incremental_runs(&self) -> Result<Vec<Run>> {
        let records = self
            .db
            .prepare("SELECT record FROM incremental_runs ORDER BY rowid")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        records
            .iter()
            .map(|r| Ok(serde_json::from_str(r)?))
            .collect()
    }
    pub(crate) fn incremental_ready(&self, r: &mut Run, d: &Value) -> Result<()> {
        self.db.execute_batch("SAVEPOINT incremental_ready")?;
        let result = (|| {
            r.state = "ready".into();
            r.applied_lsn = d["manifest"]["source"]["lsn"].as_str().map(str::to_owned);
            self.save_incremental(r)?;
            self.db.execute(
                "UPDATE publications SET descriptor=?2 WHERE export_id=?1 AND state='requested'",
                params![r.id.to_string(), d.to_string()],
            )?;
            Ok(())
        })();
        match result {
            Ok(()) => {
                self.db.execute_batch("RELEASE incremental_ready")?;
                Ok(())
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO incremental_ready; RELEASE incremental_ready")?;
                Err(e)
            }
        }
    }
    pub(crate) fn active_incremental(&self) -> Result<Vec<Run>> {
        let records=self.db.prepare("SELECT record FROM incremental_runs WHERE state IN ('requested','running','ready')")?.query_map([],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        records
            .iter()
            .map(|r| Ok(serde_json::from_str(r)?))
            .collect()
    }
    pub(crate) fn save_incremental(&self, r: &Run) -> Result<()> {
        self.db.execute(
            "UPDATE incremental_runs SET state=?2,record=?3 WHERE id=?1",
            params![r.id.to_string(), r.state, serde_json::to_string(r)?],
        )?;
        Ok(())
    }
    pub(crate) fn incremental_live(&self, r: &Run) -> Result<Capture> {
        let c = self.capture(r.project_id, r.capture_id)?;
        self.capture_live(&c)?;
        if c.desired != "running" || c.state == "resync_required" || c.state == "deleted" {
            return Err(conflict("capture generation is fenced or paused"));
        }
        if self.branch(r.branch_id)?.revision != r.source_revision {
            return Err(conflict("source revision changed"));
        }
        Ok(c)
    }
    pub(crate) fn incremental_command(
        &mut self,
        project: ProjectId,
        command: Command,
    ) -> Result<Value> {
        if let Command::Status { id } = command {
            return Ok(json!(self.incremental_run(project, id)?));
        }
        let key = match &command {
            Command::Apply { key, .. } | Command::Cancel { key, .. } => key,
            _ => unreachable!(),
        };
        if key.is_empty() || key.len() > 256 {
            return Err(invalid("incremental key requires 1–256 bytes"));
        }
        let request = serde_json::to_string(&command)?;
        let previous:Option<(String,String)>=self.db.query_row("SELECT request,response FROM incremental_requests WHERE project_id=?1 AND request_key=?2",params![project.to_string(),key],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        if let Some((old, response)) = previous {
            if old != request {
                return Err(conflict("incremental key reused"));
            }
            return Ok(serde_json::from_str(&response)?);
        }
        self.db.execute_batch("SAVEPOINT incremental_command")?;
        let result = (|| {
            let count: i64 =
                self.db
                    .query_row("SELECT count(*) FROM incremental_requests", [], |r| {
                        r.get(0)
                    })?;
            if count >= 4096 {
                return Err(conflict("incremental receipt budget exhausted"));
            }
            let value = match &command {
                Command::Apply { capture_id, .. } => {
                    let c = self.capture(project, *capture_id)?;
                    self.capture_live(&c)?;
                    if c.desired != "running"
                        || c.state != "capturing"
                        || c.observed_at_ms
                            .is_none_or(|at| now_ms().unwrap_or(i64::MAX) - at > 5000)
                    {
                        return Err(conflict(
                            "capture must have a fresh, verified bootstrap and be running",
                        ));
                    }
                    let bootstrap = c
                        .bootstrap_lsn
                        .clone()
                        .ok_or_else(|| conflict("capture bootstrap is not verified"))?;
                    if !self.active_incremental()?.is_empty() {
                        return Err(conflict(
                            "one incremental writer is admitted per installation",
                        ));
                    }
                    let count: i64 =
                        self.db
                            .query_row("SELECT count(*) FROM incremental_runs", [], |r| r.get(0))?;
                    if count >= 1024 {
                        return Err(conflict("incremental run budget exhausted"));
                    }
                    let previous:Option<(String,String)>=self.db.query_row("SELECT epoch_id,published_lsn FROM incremental_heads WHERE capture_id=?1",[c.id.to_string()],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
                    let expected: Option<String> = self
                        .db
                        .query_row(
                            "SELECT epoch_id FROM snapshot_heads WHERE branch_id=?1",
                            [c.branch_id.to_string()],
                            |r| r.get(0),
                        )
                        .optional()?;
                    if previous
                        .as_ref()
                        .is_some_and(|p| Some(&p.0) != expected.as_ref())
                    {
                        return Err(conflict(
                            "branch head changed; enroll a new capture baseline",
                        ));
                    }
                    if previous.is_none()
                        && let Some(ref old) = expected
                    {
                        let source: String = self.db.query_row(
                            "SELECT source_lsn FROM epochs WHERE id=?1",
                            [old],
                            |r| r.get(0),
                        )?;
                        if lsn(&source)? > lsn(&bootstrap)? {
                            return Err(conflict("bootstrap is older than published head"));
                        }
                    }
                    let after = previous
                        .as_ref()
                        .map(|p| p.1.clone())
                        .unwrap_or_else(|| bootstrap.clone());
                    let target = if previous.is_none() {
                        bootstrap
                    } else {
                        c.captured_lsn
                            .clone()
                            .filter(|v| lsn(v).ok() >= lsn(&after).ok())
                            .unwrap_or_else(|| after.clone())
                    };
                    let r = Run {
                        id: OperationId::new(),
                        capture_id: c.id,
                        project_id: project,
                        branch_id: c.branch_id,
                        epoch_id: EpochId::new(),
                        previous_epoch: previous.map(|p| parse(&p.0)).transpose()?,
                        expected_head: expected.map(|s| parse(&s)).transpose()?,
                        source_revision: self.branch(c.branch_id)?.revision,
                        after_lsn: after,
                        target_lsn: target,
                        applied_lsn: None,
                        state: "requested".into(),
                        error: None,
                        created_at_ms: now_ms()?,
                        deadline_ms: now_ms()? + 300000,
                        worker_generation: 0,
                        started_at_ms: None,
                        attempts: 0,
                    };
                    self.db.execute("INSERT INTO analytical_artifacts(id,project_id,branch_id,kind) VALUES (?1,?2,?3,'incremental')",params![r.id.to_string(),project.to_string(),r.branch_id.to_string()])?;
                    let order: i64 = self.db.query_row(
                        "SELECT ordinal FROM analytical_artifacts WHERE id=?1",
                        [r.id.to_string()],
                        |r| r.get(0),
                    )?;
                    self.db.execute("INSERT INTO publications(export_id,epoch_id,branch_id,source_revision,export_order,requested_at_ms,state) VALUES (?1,?2,?3,?4,?5,?6,'requested')",params![r.id.to_string(),r.epoch_id.to_string(),r.branch_id.to_string(),r.source_revision,order,r.created_at_ms])?;
                    self.db.execute(
                        "INSERT INTO incremental_runs VALUES (?1,?2,?3,?4)",
                        params![
                            r.id.to_string(),
                            c.id.to_string(),
                            r.state,
                            serde_json::to_string(&r)?
                        ],
                    )?;
                    json!(r)
                }
                Command::Cancel { id, .. } => {
                    let mut r = self.incremental_run(project, *id)?;
                    if matches!(r.state.as_str(), "requested" | "running" | "ready") {
                        self.fail_incremental(&mut r, "cancelled", true)?;
                    }
                    json!(r)
                }
                _ => unreachable!(),
            };
            self.db.execute(
                "INSERT INTO incremental_requests VALUES (?1,?2,?3,?4)",
                params![project.to_string(), key, request, value.to_string()],
            )?;
            Ok(value)
        })();
        match result {
            Ok(v) => {
                self.db.execute_batch("RELEASE incremental_command")?;
                Ok(v)
            }
            Err(e) => {
                self.db.execute_batch(
                    "ROLLBACK TO incremental_command; RELEASE incremental_command",
                )?;
                Err(e)
            }
        }
    }
    pub(crate) fn fail_incremental(&self, r: &mut Run, code: &str, cancel: bool) -> Result<()> {
        self.db.execute_batch("SAVEPOINT incremental_fail")?;
        let result = (|| {
            r.state = if cancel { "cancelled" } else { "failed" }.into();
            r.error = Some(code.into());
            self.save_incremental(r)?;
            self.db.execute("UPDATE publications SET state=?2,error=?3 WHERE export_id=?1 AND state IN ('requested','files_complete')",params![r.id.to_string(),r.state,code])?;
            self.db.execute(
                "INSERT INTO analytics_gc VALUES (?1,NULL,'pending') ON CONFLICT DO NOTHING",
                [r.id.to_string()],
            )?;
            // A partial table commit requires reconciliation under the SAME run;
            // abandoning that run fences the generation, preserving published maps.
            let mut c = self.capture(r.project_id, r.capture_id)?;
            if matches!(c.desired.as_str(), "running" | "paused") {
                c.desired = "fenced".into();
                c.state = "resync_required".into();
                c.error = Some(code.into());
                c.cleanup_complete = false;
                self.save_capture(&c)?;
            }
            Ok(())
        })();
        match result {
            Ok(()) => {
                self.db.execute_batch("RELEASE incremental_fail")?;
                Ok(())
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO incremental_fail; RELEASE incremental_fail")?;
                Err(e)
            }
        }
    }
    pub(crate) fn commit_incremental(&mut self, r: &mut Run, descriptor: &Value) -> Result<()> {
        let live = self.incremental_run(r.project_id, r.id)?;
        if live.state == "succeeded" {
            *r = live;
            return Ok(());
        }
        if !matches!(live.state.as_str(), "running" | "ready") {
            return Err(conflict("incremental run fenced"));
        }
        let c = self.incremental_live(r)?;
        if c.state != "capturing"
            || c.observed_at_ms
                .is_none_or(|at| now_ms().unwrap_or(i64::MAX) - at > 5000)
        {
            return Err(conflict("capture status is not current"));
        }
        let end = descriptor["manifest"]["source"]["lsn"]
            .as_str()
            .ok_or_else(|| invalid("missing applied LSN"))?;
        if lsn(end)? < lsn(&r.after_lsn)? || lsn(end)? > lsn(&r.target_lsn)? {
            return Err(conflict("applied boundary outside admitted prefix"));
        }
        let current: Option<String> = self
            .db
            .query_row(
                "SELECT epoch_id FROM snapshot_heads WHERE branch_id=?1",
                [r.branch_id.to_string()],
                |r| r.get(0),
            )
            .optional()?;
        if current != r.expected_head.map(|v| v.to_string()) {
            return Err(conflict("snapshot head changed during application"));
        }
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO epochs VALUES (?1,?2,?3)",
            params![r.epoch_id.to_string(), r.branch_id.to_string(), end],
        )?;
        for t in descriptor["manifest"]["tables"]
            .as_array()
            .ok_or_else(|| invalid("missing version map"))?
        {
            tx.execute(
                "INSERT INTO table_mappings VALUES (?1,?2,?3,?4)",
                params![
                    r.epoch_id.to_string(),
                    t["oid"].as_i64().ok_or_else(|| invalid("missing OID"))?,
                    json!([t["schema"], t["name"]]).to_string(),
                    format!(
                        "{}/{}",
                        descriptor["generation"].as_str().unwrap(),
                        t["path"].as_str().unwrap()
                    )
                ],
            )?;
        }
        tx.execute(
            "INSERT INTO snapshots VALUES (?1,?2,'available',NULL)",
            params![r.epoch_id.to_string(), r.id.to_string()],
        )?;
        tx.execute("INSERT INTO snapshot_heads VALUES (?1,?2) ON CONFLICT(branch_id) DO UPDATE SET epoch_id=excluded.epoch_id",params![r.branch_id.to_string(),r.epoch_id.to_string()])?;
        tx.execute("INSERT INTO incremental_heads VALUES (?1,?2,?3) ON CONFLICT(capture_id) DO UPDATE SET epoch_id=excluded.epoch_id,published_lsn=excluded.published_lsn",params![r.capture_id.to_string(),r.epoch_id.to_string(),end])?;
        tx.execute("UPDATE publications SET state='published',published_at_ms=?2,descriptor=?3 WHERE export_id=?1",params![r.id.to_string(),now_ms()?,descriptor.to_string()])?;
        r.state = "succeeded".into();
        r.applied_lsn = Some(end.into());
        tx.execute(
            "UPDATE incremental_runs SET state='succeeded',record=?2 WHERE id=?1",
            params![r.id.to_string(), serde_json::to_string(r)?],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub(crate) fn artifact_in_project(&self, project: ProjectId, id: OperationId) -> Result<()> {
        if !self
            .db
            .prepare("SELECT 1 FROM analytical_artifacts WHERE id=?1 AND project_id=?2")?
            .exists(params![id.to_string(), project.to_string()])?
        {
            return Err(missing("analytical artifact in project"));
        }
        Ok(())
    }
    pub(crate) fn is_incremental_artifact(&self, id: OperationId) -> Result<bool> {
        Ok(self
            .db
            .prepare("SELECT 1 FROM analytical_artifacts WHERE id=?1 AND kind='incremental'")?
            .exists([id.to_string()])?)
    }
    pub(crate) fn incremental_root_identity(&self, id: OperationId) -> Result<Option<Value>> {
        let record: Option<String> = self
            .db
            .query_row(
                "SELECT record FROM sync_captures WHERE id=?1",
                [id.to_string()],
                |r| r.get(0),
            )
            .optional()?;
        record
            .map(|s| Ok(serde_json::from_str::<Capture>(&s)?.identity))
            .transpose()
    }
    pub(crate) fn incremental_root_referenced(&self, capture: OperationId) -> Result<bool> {
        Ok(self.db.prepare("SELECT 1 FROM sync_captures WHERE id=?1 AND state!='deleted' UNION ALL SELECT 1 FROM incremental_runs r LEFT JOIN publications p ON p.export_id=r.id LEFT JOIN snapshots s ON s.epoch_id=p.epoch_id WHERE r.capture_id=?1 AND (r.state IN ('requested','running','ready') OR s.state IN ('available','unavailable','deleting'))")?.exists([capture.to_string()])?)
    }
}
pub(crate) fn restore(db: &rusqlite::Connection) -> Result<()> {
    db.execute_batch("INSERT INTO analytics_gc SELECT id,NULL,'pending' FROM incremental_runs WHERE state IN ('requested','running','ready') ON CONFLICT DO NOTHING; UPDATE publications SET state='cancelled',error='restored_requires_resync' WHERE export_id IN (SELECT id FROM incremental_runs WHERE state IN ('requested','running','ready')); UPDATE incremental_runs SET state='cancelled',record=json_set(record,'$.state','cancelled','$.error','restored_requires_resync') WHERE state IN ('requested','running','ready');")?;
    Ok(())
}
