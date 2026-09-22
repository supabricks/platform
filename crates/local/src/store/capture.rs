use super::{
    Result, Store,
    error::{conflict, invalid, missing},
};
use crate::capture::{Capture, Command};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use supabricks_core::resource::{OperationId, ProjectId};

impl Store {
    pub fn capture(&self, project: ProjectId, id: OperationId) -> Result<Capture> {
        let text: String = self
            .db
            .query_row(
                "SELECT record FROM sync_captures WHERE project_id=?1 AND id=?2",
                params![project.to_string(), id.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("capture generation in project"))?;
        Ok(serde_json::from_str(&text)?)
    }
    pub(crate) fn captures(&self) -> Result<Vec<Capture>> {
        let rows = self
            .db
            .prepare("SELECT record FROM sync_captures WHERE state!='deleted'")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows.iter().map(|s| Ok(serde_json::from_str(s)?)).collect()
    }
    pub(crate) fn save_capture(&self, c: &Capture) -> Result<()> {
        self.db.execute(
            "UPDATE sync_captures SET state=?2,bootstrap_id=?3,record=?4 WHERE id=?1",
            params![
                c.id.to_string(),
                c.state,
                c.bootstrap_id.map(|id| id.to_string()),
                serde_json::to_string(c)?
            ],
        )?;
        Ok(())
    }
    pub(crate) fn capture_live(&self, c: &Capture) -> Result<()> {
        let p = self.sync_policy(c.project_id, c.policy_id)?;
        if if c.identity["decoder_version"] == 2 {
            !p.config.triggered() || !matches!(p.state.as_str(), "active" | "paused")
        } else {
            p.revision != c.policy_revision || p.state != "active" || p.config.schedule.is_some()
        } {
            return Err(conflict("capture policy changed"));
        }
        self.sync_source(&p)?;
        Ok(())
    }
    pub(crate) fn capture_command(
        &mut self,
        project: ProjectId,
        command: Command,
    ) -> Result<Value> {
        let request = serde_json::to_string(&command)?;
        if let Some(key) = command.key() {
            if key.is_empty() || key.len() > 256 {
                return Err(invalid("capture retry key requires 1–256 bytes"));
            }
            let old:Option<(String,String)>=self.db.query_row("SELECT request,response FROM capture_requests WHERE project_id=?1 AND request_key=?2",params![project.to_string(),key],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            if let Some((previous, response)) = old {
                if previous != request {
                    return Err(conflict("capture key reused with different parameters"));
                }
                return Ok(serde_json::from_str(&response)?);
            }
            let count: i64 =
                self.db
                    .query_row("SELECT count(*) FROM capture_requests", [], |r| r.get(0))?;
            if count >= 10000 {
                return Err(conflict("capture receipt journal full"));
            }
        }
        self.db.execute_batch("SAVEPOINT capture_command")?;
        let result = (|| {
            let value = match &command {
                Command::Start {
                    policy_id,
                    expected_revision,
                    limits,
                    ..
                } => {
                    limits.validate()?;
                    let p = self.sync_policy(project, *policy_id)?;
                    if p.revision != *expected_revision
                        || p.state != "active"
                        || (p.config.schedule.is_some() && !p.config.triggered())
                    {
                        return Err(conflict(
                            "capture requires an active manual snapshot or triggered policy and current revision",
                        ));
                    }
                    self.sync_source(&p)?;
                    if self.branch(p.branch_id)?.endpoint.desired_state
                        != supabricks_core::resource::DesiredState::Running
                    {
                        return Err(conflict("resume the source branch before starting capture"));
                    }
                    if !self.captures()?.is_empty() {
                        return Err(conflict(
                            "one capture generation is allowed per installation; delete the old generation before resync",
                        ));
                    }
                    let count: i64 =
                        self.db
                            .query_row("SELECT count(*) FROM sync_captures", [], |r| r.get(0))?;
                    if count >= 128 {
                        return Err(conflict("capture generation journal full"));
                    }
                    let id = OperationId::new();
                    let c = Capture {
                        id,
                        policy_id: p.id,
                        policy_revision: p.revision,
                        project_id: project,
                        branch_id: p.branch_id,
                        identity: json!({"installation_id":p.installation_id,"deployment_id":p.deployment_id,"project_id":project,"branch_id":p.branch_id,"tenant_id":p.tenant_id,"timeline_id":p.timeline_id,"database":"postgres","policy_revision":p.revision,"generation":id,"decoder_version":if p.config.triggered(){2}else{1},"spool_format_version":1}),
                        limits: limits.clone(),
                        desired: "running".into(),
                        state: "requested".into(),
                        error: None,
                        created_at_ms: super::now_ms()?,
                        worker_generation: 0,
                        bootstrap_id: None,
                        bootstrap_lsn: None,
                        barrier: None,
                        observed_at_ms: None,
                        start_lsn: None,
                        captured_lsn: None,
                        source_lsn: None,
                        retained_wal_bytes: None,
                        spool_bytes: None,
                        cleanup_complete: false,
                    };
                    self.db.execute(
                        "INSERT INTO sync_captures VALUES (?1,?2,?3,?4,?5,NULL,?6)",
                        params![
                            id.to_string(),
                            p.id.to_string(),
                            project.to_string(),
                            p.branch_id.to_string(),
                            c.state,
                            serde_json::to_string(&c)?
                        ],
                    )?;
                    if p.config.triggered() {
                        self.db.execute("UPDATE sync_policies SET record=json_set(record,'$.capture_id',?2) WHERE id=?1",params![p.id.to_string(),c.id.to_string()])?;
                    }
                    json!(c)
                }
                Command::Status { id } => {
                    let mut c = self.capture(project, *id)?;
                    if matches!(c.state.as_str(), "capturing" | "bootstrapping" | "paused")
                        && c.observed_at_ms
                            .is_none_or(|at| super::now_ms().unwrap_or(i64::MAX) - at > 5000)
                    {
                        c.state = "unavailable".into();
                        c.error = Some("worker_status_stale".into());
                    }
                    json!(c)
                }
                Command::Pause { id, .. }
                | Command::Resume { id, .. }
                | Command::Delete { id, .. } => {
                    let mut c = self.capture(project, *id)?;
                    if matches!(command, Command::Delete { .. }) {
                        if c.state != "deleted" {
                            c.desired = "deleted".into();
                            c.state = "deleting".into();
                            c.cleanup_complete = false;
                        }
                    } else {
                        if matches!(c.state.as_str(), "resync_required" | "deleting" | "deleted")
                            || c.desired == "fenced"
                        {
                            return Err(conflict(
                                "generation requires explicit delete and new bootstrap",
                            ));
                        }
                        self.capture_live(&c)?;
                        c.desired = if matches!(command, Command::Pause { .. }) {
                            "paused"
                        } else {
                            "running"
                        }
                        .into();
                        c.state = if c.desired == "paused" {
                            "pausing"
                        } else {
                            "recovering"
                        }
                        .into();
                    }
                    self.save_capture(&c)?;
                    json!(c)
                }
            };
            if let Some(key) = command.key() {
                self.db.execute(
                    "INSERT INTO capture_requests VALUES (?1,?2,?3,?4)",
                    params![project.to_string(), key, request, value.to_string()],
                )?;
            }
            Ok(value)
        })();
        match result {
            Ok(v) => {
                self.db.execute_batch("RELEASE capture_command")?;
                Ok(v)
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO capture_command; RELEASE capture_command")?;
                Err(e)
            }
        }
    }
    pub(crate) fn capture_bootstrap_protected(&self, id: OperationId) -> Result<bool> {
        Ok(self
            .db
            .prepare("SELECT 1 FROM sync_captures c JOIN operations o ON o.id=?1 WHERE c.state!='deleted' AND (c.bootstrap_id=o.id OR o.request_key='internal:capture-bootstrap:' || c.id)")?
            .exists([id.to_string()])?)
    }
    pub(crate) fn recover_captures(&self) -> Result<()> {
        for mut c in self.captures()? {
            if matches!(c.desired.as_str(), "running" | "paused") {
                c.state = "recovering".into();
                c.observed_at_ms = None;
                self.save_capture(&c)?;
            }
        }
        Ok(())
    }
    pub(crate) fn capture_wal_budget(
        &self,
        branch: supabricks_core::resource::BranchId,
    ) -> Result<Option<u64>> {
        Ok(self
            .captures()?
            .into_iter()
            .find(|c| c.branch_id == branch)
            .map(|c| c.limits.wal_bytes))
    }
}

impl Store {
    pub(crate) fn capture_bootstrap_for_key(&self, c: &Capture) -> Result<Option<OperationId>> {
        let id: Option<String> = self
            .db
            .query_row(
                "SELECT id FROM operations WHERE project_id=?1 AND request_key=?2",
                params![
                    c.project_id.to_string(),
                    format!("internal:capture-bootstrap:{}", c.id)
                ],
                |r| r.get(0),
            )
            .optional()?;
        id.map(|s| super::parse(&s)).transpose()
    }
    pub(crate) fn capture_lease(&self, c: &Capture, active: bool) -> Result<()> {
        if active {
            self.db.execute("INSERT INTO leases VALUES (?1,?2,NULL,?3,?4,?5) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,expires_at_ms=excluded.expires_at_ms",params![c.id.to_string(),c.branch_id.to_string(),format!("capture:{}",c.id),self.generation(),super::now_ms()?+60000])?;
        } else {
            self.db.execute(
                "DELETE FROM leases WHERE id=?1 AND holder=?2",
                params![c.id.to_string(), format!("capture:{}", c.id)],
            )?;
        }
        Ok(())
    }
    pub(crate) fn finish_capture_delete(&self, c: &mut Capture) -> Result<()> {
        self.db.execute_batch("SAVEPOINT capture_deleted")?;
        let result = (|| {
            c.state = "deleted".into();
            self.save_capture(c)?;
            self.capture_lease(c, false)?;
            if let Some(id) = c.bootstrap_id {
                self.db.execute(
                    "INSERT INTO analytics_gc VALUES (?1,NULL,'pending') ON CONFLICT DO NOTHING",
                    [id.to_string()],
                )?;
            }
            Ok(())
        })();
        match result {
            Ok(()) => {
                self.db.execute_batch("RELEASE capture_deleted")?;
                Ok(())
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO capture_deleted; RELEASE capture_deleted")?;
                Err(e)
            }
        }
    }
}

/// Restored source history/cursors require explicit re-enrollment, never automatic capture.
pub(crate) fn restore(db: &rusqlite::Connection) -> Result<()> {
    db.execute_batch("UPDATE sync_captures SET state='resync_required',record=json_set(record,'$.state','resync_required','$.desired','fenced','$.error','restored_requires_resync','$.observed_at_ms',NULL,'$.cleanup_complete',json('true')) WHERE state!='deleted';")?;
    Ok(())
}
