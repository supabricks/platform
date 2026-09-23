use super::{
    Result, Store,
    error::{conflict, invalid, missing},
};
use crate::sync::{Command, Policy, Run};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use supabricks_core::resource::{DeploymentId, DesiredState, OperationId, ProjectId};

impl Store {
    pub fn sync_policy(&self, project: ProjectId, id: OperationId) -> Result<Policy> {
        let text: String = self
            .db
            .query_row(
                "SELECT record FROM sync_policies WHERE project_id=?1 AND id=?2",
                params![project.to_string(), id.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("snapshot policy in project"))?;
        Ok(serde_json::from_str(&text)?)
    }
    pub fn sync_run(&self, project: ProjectId, id: OperationId) -> Result<Run> {
        let text: String = self
            .db
            .query_row(
                "SELECT record FROM sync_runs WHERE project_id=?1 AND id=?2",
                params![project.to_string(), id.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("snapshot run in project"))?;
        Ok(serde_json::from_str(&text)?)
    }
    fn sync_policies(&self) -> Result<Vec<Policy>> {
        let rows = self
            .db
            .prepare("SELECT record FROM sync_policies ORDER BY rowid")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows.iter().map(|s| Ok(serde_json::from_str(s)?)).collect()
    }
    fn active_sync_runs(&self) -> Result<Vec<Run>> {
        let rows = self.db.prepare("SELECT record FROM sync_runs WHERE state IN ('queued','starting','running') ORDER BY ordinal")?.query_map([], |r| r.get::<_, String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        rows.iter().map(|s| Ok(serde_json::from_str(s)?)).collect()
    }
    fn save_sync_policy(&self, p: &Policy) -> Result<()> {
        self.db.execute(
            "UPDATE sync_policies SET state=?2,record=?3 WHERE id=?1",
            params![p.id.to_string(), p.state, serde_json::to_string(p)?],
        )?;
        Ok(())
    }
    fn save_sync_run(&self, r: &Run) -> Result<()> {
        self.db.execute(
            "UPDATE sync_runs SET state=?2,refresh_id=?3,record=?4 WHERE id=?1",
            params![
                r.id.to_string(),
                r.state,
                r.refresh_id.map(|i| i.to_string()),
                serde_json::to_string(r)?
            ],
        )?;
        Ok(())
    }
    pub(crate) fn sync_source(&self, p: &Policy) -> Result<()> {
        let b = self.branch_in_project(p.project_id, p.branch_id)?;
        let d = self.deployment(p.deployment_id)?;
        if self.is_export(p.branch_id)?
            || b.expired
            || b.endpoint.desired_state == DesiredState::Deleted
            || self.installation_id()? != p.installation_id
            || b.branch.tenant_id.to_string() != p.tenant_id
            || b.branch.timeline_id.to_string() != p.timeline_id
            || d.runtime_project_id != p.project_id
        {
            return Err(conflict(
                "snapshot source unavailable, changed lineage, or requires governed authority",
            ));
        }
        self.sync_authority_live(p)?;
        Ok(())
    }
    fn sync_expected(&self, project: ProjectId, id: OperationId, revision: i64) -> Result<Policy> {
        let p = self.sync_policy(project, id)?;
        if p.state == "deleted" || p.revision != revision {
            return Err(conflict(
                "snapshot policy revision changed or policy deleted",
            ));
        }
        Ok(p)
    }
    fn admit_sync_run(&self, p: &Policy, trigger: &str, due: Option<i64>, now: i64) -> Result<Run> {
        self.sync_source(p)?;
        if p.state != "active" || p.pause_requested {
            return Err(conflict("snapshot policy is not active"));
        }
        if self.db.prepare("SELECT 1 FROM sync_runs WHERE branch_id=?1 AND state IN ('queued','starting','running')")?.exists([p.branch_id.to_string()])? {
            return Err(conflict("snapshot policy already has an active run"));
        }
        let total: i64 = self
            .db
            .query_row("SELECT count(*) FROM sync_runs", [], |r| r.get(0))?;
        if total >= 10_000 || self.active_sync_runs()?.len() >= 32 {
            return Err(conflict("snapshot run journal or queue limit reached"));
        }
        let r = Run {
            id: OperationId::new(),
            policy_id: p.id,
            policy_revision: p.revision,
            config: p.config.clone(),
            project_id: p.project_id,
            branch_id: p.branch_id,
            trigger: trigger.into(),
            capture_id: if p.config.incremental() {
                Some(self.triggered_capture(p)?.id)
            } else {
                None
            },
            target_lsn: None,
            apply_id: None,
            published_artifact_id: None,
            batches: 0,
            deadline_ms: if p.config.incremental() {
                Some(now.saturating_add(p.config.limits.timeout_ms as i64))
            } else {
                None
            },
            state: "queued".into(),
            admitted_at_ms: now,
            scheduled_for_ms: due,
            finished_at_ms: None,
            refresh_id: None,
            epoch_id: None,
            source_lsn: None,
            error: None,
        };
        self.db.execute("INSERT INTO sync_runs(id,policy_id,project_id,branch_id,state,record) VALUES (?1,?2,?3,?4,?5,?6)", params![r.id.to_string(),p.id.to_string(),p.project_id.to_string(),p.branch_id.to_string(),r.state,serde_json::to_string(&r)?])?;
        Ok(r)
    }
    // Runs under the command/reconciliation savepoint. Fence publication immediately;
    // A01 still owns asynchronous worker termination and hidden-branch cleanup.
    fn cancel_sync_run(&self, mut r: Run, reason: &str, now: i64) -> Result<Run> {
        if r.state == "succeeded" {
            return Err(conflict("published snapshot cannot be cancelled"));
        }
        if matches!(r.state.as_str(), "failed" | "cancelled") {
            return Ok(r);
        }
        if r.config.incremental() {
            self.cancel_triggered_apply(&r, reason)?;
        }
        if let Some(id) = r.refresh_id {
            if self
                .db
                .prepare("SELECT 1 FROM publications WHERE export_id=?1 AND state='published'")?
                .exists([id.to_string()])?
            {
                return Err(conflict("published snapshot cannot be cancelled"));
            }
            self.db.execute(
                "UPDATE exports SET cancel_requested=1 WHERE id=?1",
                [id.to_string()],
            )?;
            self.db.execute("INSERT INTO analytical_refreshes(export_id,error) VALUES (?1,'cancelled') ON CONFLICT(export_id) DO UPDATE SET error='cancelled'", [id.to_string()])?;
            self.db.execute("UPDATE publications SET state='cancelled',error='snapshot_run_fenced' WHERE export_id=?1 AND state IN ('requested','files_complete')", [id.to_string()])?;
            self.db.execute("INSERT INTO analytics_gc SELECT id,NULL,'pending' FROM exports WHERE id=?1 AND state IN ('complete','failed','cancelled') ON CONFLICT DO NOTHING", [id.to_string()])?;
        }
        r.state = "cancelled".into();
        r.error = Some(reason.into());
        r.finished_at_ms = Some(now);
        self.save_sync_run(&r)?;
        if r.config.continuous() && reason == "user_cancelled" {
            let mut p = self.sync_policy(r.project_id, r.policy_id)?;
            p.state = "paused".into();
            p.pause_requested = false;
            self.save_sync_policy(&p)?;
        }
        Ok(r)
    }
    pub(crate) fn sync_command(
        &mut self,
        project: ProjectId,
        deployment: DeploymentId,
        command: Command,
        now: i64,
    ) -> Result<Value> {
        self.sync_command_authority(project, deployment, command, now, None)
    }
    pub(crate) fn sync_command_authority(
        &mut self,
        project: ProjectId,
        deployment: DeploymentId,
        command: Command,
        now: i64,
        authority: Option<crate::sync::ServiceAuthority>,
    ) -> Result<Value> {
        if self.deployment(deployment)?.runtime_project_id != project {
            return Err(missing("deployment in project"));
        }
        self.reconcile_sync(now)?;
        let request = serde_json::to_string(&command)?;
        if let Some(key) = command.key() {
            if key.is_empty() || key.len() > 256 {
                return Err(invalid("snapshot request key requires 1–256 bytes"));
            }
            let old: Option<(String,String)> = self.db.query_row("SELECT request,response FROM sync_requests WHERE project_id=?1 AND request_key=?2", params![project.to_string(),key], |r|Ok((r.get(0)?,r.get(1)?))).optional()?;
            if let Some((previous, response)) = old {
                if previous != request {
                    return Err(conflict("snapshot key reused with different parameters"));
                }
                return Ok(serde_json::from_str(&response)?);
            }
            let count: i64 = self
                .db
                .query_row("SELECT count(*) FROM sync_requests", [], |r| r.get(0))?;
            if count >= 20_000 {
                return Err(conflict("snapshot request journal limit reached"));
            }
        }
        self.db.execute_batch("SAVEPOINT sync_command")?;
        let result = (|| {
            let result = match &command {
                Command::Inspect { branch } => self.inspect_sync(project, deployment, branch)?,
                Command::ReviewResync { id } => self.review_sync_resync(project, *id)?,
                Command::Resync {
                    id,
                    expected_revision,
                    review_hash,
                    ..
                } => {
                    let mut p = self.sync_expected(project, *id, *expected_revision)?;
                    if self.review_sync_resync(project, *id)?["review_hash"] != *review_hash {
                        return Err(conflict("resync review changed; inspect and review again"));
                    }
                    for r in self
                        .active_sync_runs()?
                        .into_iter()
                        .filter(|r| r.policy_id == p.id)
                    {
                        self.cancel_sync_run(r, "reviewed_resync", now)?;
                    }
                    self.delete_policy_capture(&p)?;
                    p.revision += 1;
                    p.state = "paused".into();
                    p.pause_requested = false;
                    p.observation = None;
                    p.next_due_at_ms = None;
                    p.error = Some("resync_cleanup_pending".into());
                    self.save_sync_policy(&p)?;
                    self.sync_policy_view(&p, now)?
                }
                Command::Create { branch, config, .. } => {
                    config.validate()?;
                    let branch_id = crate::api::resolve(
                        self,
                        &crate::api::Binding {
                            project_id: project,
                            worktree: self.root().to_owned(),
                        },
                        Some(branch),
                    )?;
                    let b = self.branch(branch_id)?;
                    if self.sync_policies()?.len() >= 128 {
                        return Err(conflict("snapshot policy journal limit reached"));
                    }
                    if self
                        .db
                        .prepare(
                            "SELECT 1 FROM sync_policies WHERE branch_id=?1 AND state!='deleted'",
                        )?
                        .exists([branch_id.to_string()])?
                    {
                        return Err(conflict("source group already has a snapshot policy"));
                    }
                    let p = Policy {
                        service_authority: authority.clone(),
                        id: OperationId::new(),
                        capture_id: None,
                        pause_requested: false,
                        observation: None,
                        project_id: project,
                        deployment_id: deployment,
                        branch_id,
                        installation_id: self.installation_id()?,
                        tenant_id: b.branch.tenant_id.to_string(),
                        timeline_id: b.branch.timeline_id.to_string(),
                        database: "postgres".into(),
                        membership: "all_supported_application_tables_at_snapshot".into(),
                        authority: if authority.is_some() {
                            "scoped_service"
                        } else {
                            "local_owner"
                        }
                        .into(),
                        revision: 1,
                        state: "active".into(),
                        config: config.clone(),
                        created_at_ms: now,
                        next_due_at_ms: config.next_due(now),
                        last_success_at_ms: None,
                        last_epoch_id: None,
                        error: None,
                    };
                    self.sync_source(&p)?;
                    self.db.execute(
                        "INSERT INTO sync_policies VALUES (?1,?2,?3,?4,?5)",
                        params![
                            p.id.to_string(),
                            project.to_string(),
                            branch_id.to_string(),
                            p.state,
                            serde_json::to_string(&p)?
                        ],
                    )?;
                    if p.config.incremental() {
                        self.enroll_triggered(&p)?;
                    }
                    self.sync_policy_view(&self.sync_policy(project, p.id)?, now)?
                }
                Command::Update {
                    id,
                    expected_revision,
                    ..
                }
                | Command::Pause {
                    id,
                    expected_revision,
                    ..
                }
                | Command::Resume {
                    id,
                    expected_revision,
                    ..
                }
                | Command::Delete {
                    id,
                    expected_revision,
                    ..
                } => {
                    let mut p = self.sync_expected(project, *id, *expected_revision)?;
                    if p.pause_requested && !matches!(command, Command::Delete { .. }) {
                        return Err(conflict(
                            "wait for continuous pause to reach a batch boundary",
                        ));
                    }
                    let graceful =
                        p.config.continuous() && matches!(command, Command::Pause { .. });
                    if let Command::Update { config, .. } = &command {
                        config.validate()?;
                        self.sync_source(&p)?;
                        if config.mode != p.config.mode
                            && config.incremental()
                            && p.config.incremental()
                            && self.active_sync_runs()?.iter().any(|r| r.policy_id == p.id)
                        {
                            return Err(conflict(
                                "pause and drain before changing incremental mode",
                            ));
                        }
                        if config.incremental() != p.config.incremental()
                            && self.captures()?.iter().any(|c| c.policy_id == p.id)
                        {
                            return Err(conflict("delete capture before changing sync mode"));
                        }
                        p.config = config.clone();
                        p.error = None;
                    }
                    if matches!(command, Command::Resume { .. }) {
                        self.sync_source(&p)?;
                        if self
                            .captures()?
                            .iter()
                            .any(|c| c.policy_id == p.id && c.desired == "deleted")
                        {
                            return Err(conflict(
                                "resync cleanup is still pending; inspect status before resuming",
                            ));
                        }
                        p.state = "active".into();
                        p.error = None;
                    }
                    if matches!(command, Command::Pause { .. }) {
                        p.state = "paused".into();
                    }
                    if matches!(command, Command::Delete { .. }) {
                        p.state = "deleted".into();
                        p.pause_requested = false;
                    }
                    for r in self
                        .active_sync_runs()?
                        .into_iter()
                        .filter(|r| r.policy_id == p.id)
                    {
                        if graceful && r.apply_id.is_some() {
                            let mut r = r;
                            r.policy_revision = p.revision + 1;
                            self.save_sync_run(&r)?;
                            p.state = "active".into();
                            p.pause_requested = true;
                        } else {
                            self.cancel_sync_run(r, "policy_revised", now)?;
                        }
                    }
                    p.revision += 1;
                    p.next_due_at_ms = if p.state == "active" {
                        p.config.next_due(now)
                    } else {
                        None
                    };
                    self.save_sync_policy(&p)?;
                    if p.state == "deleted" {
                        self.delete_policy_capture(&p)?;
                    }
                    if p.config.incremental()
                        && p.state == "active"
                        && matches!(command, Command::Update { .. } | Command::Resume { .. })
                        && !self.captures()?.iter().any(|c| c.policy_id == p.id)
                    {
                        self.enroll_triggered(&p)?;
                    }
                    self.sync_policy_view(&self.sync_policy(project, p.id)?, now)?
                }
                Command::RunNow {
                    id,
                    expected_revision,
                    ..
                } => {
                    let p = self.sync_expected(project, *id, *expected_revision)?;
                    if p.config.continuous() {
                        return Err(conflict(
                            "continuous runs are supervised; use pause or resume",
                        ));
                    }
                    json!(self.admit_sync_run(&p, "manual", None, now)?)
                }
                Command::Cancel { id, .. } => json!(self.cancel_sync_run(
                    self.sync_run(project, *id)?,
                    "user_cancelled",
                    now
                )?),
                Command::Get { id } => {
                    self.sync_policy_view(&self.sync_policy(project, *id)?, now)?
                }
                Command::List => json!(
                    self.sync_policies()?
                        .into_iter()
                        .filter(|p| p.project_id == project)
                        .map(|p| self.sync_policy_view(&p, now))
                        .collect::<Result<Vec<_>>>()?
                ),
                Command::Run { id } => json!(self.sync_run(project, *id)?),
                Command::Runs { id, limit } => {
                    self.sync_policy(project, *id)?;
                    if !(1..=100).contains(limit) {
                        return Err(invalid("run history limit requires 1–100"));
                    }
                    let rows = self.db.prepare("SELECT record FROM sync_runs WHERE policy_id=?1 ORDER BY ordinal DESC LIMIT ?2")?.query_map(params![id.to_string(),*limit as i64],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
                    json!(
                        rows.iter()
                            .map(|s| serde_json::from_str::<Run>(s))
                            .collect::<std::result::Result<Vec<_>, _>>()?
                    )
                }
            };
            if let Some(key) = command.key() {
                self.db.execute(
                    "INSERT INTO sync_requests VALUES (?1,?2,?3,?4)",
                    params![project.to_string(), key, request, result.to_string()],
                )?;
            }
            Ok(result)
        })();
        match result {
            Ok(value) => {
                self.db.execute_batch("RELEASE sync_command")?;
                Ok(value)
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO sync_command; RELEASE sync_command")?;
                Err(e)
            }
        }
    }
    pub(crate) fn schedule_sync(&mut self, now: i64) -> Result<()> {
        for mut p in self
            .sync_policies()?
            .into_iter()
            .filter(|p| p.state == "active" && p.next_due_at_ms.is_some_and(|t| t <= now))
        {
            self.db.execute_batch("SAVEPOINT sync_schedule")?;
            let result = (|| {
                let due = p.next_due_at_ms.unwrap();
                // Advance from the previous due time, preserving phase, skipping all missed intervals.
                let interval = p
                    .config
                    .schedule
                    .as_ref()
                    .ok_or_else(|| invalid("scheduled policy has no interval"))?
                    .interval_seconds as i64
                    * 1000;
                let next =
                    due.saturating_add(((now - due) / interval + 1).saturating_mul(interval));
                if !self.active_sync_runs()?.iter().any(|r| r.policy_id == p.id) {
                    match self.admit_sync_run(&p, "schedule", Some(due), now) {
                        Ok(_) => p.error = None,
                        Err(_) => p.error = Some("scheduled_admission_unavailable".into()),
                    }
                }
                p.next_due_at_ms = Some(next);
                self.save_sync_policy(&p)
            })();
            match result {
                Ok(()) => self.db.execute_batch("RELEASE sync_schedule")?,
                Err(e) => {
                    self.db
                        .execute_batch("ROLLBACK TO sync_schedule; RELEASE sync_schedule")?;
                    return Err(e);
                }
            }
        }
        Ok(())
    }
    pub(crate) fn next_sync_run(&self) -> Result<Option<Run>> {
        Ok(self
            .active_sync_runs()?
            .into_iter()
            .find(|r| !r.config.incremental() && (r.state == "queued" || r.state == "starting")))
    }
    pub(crate) fn start_sync_run(&self, id: OperationId) -> Result<()> {
        let mut r = self
            .active_sync_runs()?
            .into_iter()
            .find(|r| r.id == id)
            .ok_or_else(|| missing("active snapshot run"))?;
        r.state = "starting".into();
        self.save_sync_run(&r)
    }
    pub(crate) fn link_sync_refresh(
        &mut self,
        id: OperationId,
        refresh: OperationId,
    ) -> Result<()> {
        let mut r = self
            .active_sync_runs()?
            .into_iter()
            .find(|r| r.id == id)
            .ok_or_else(|| missing("active snapshot run"))?;
        let e = self.export_in_project(r.project_id, refresh)?;
        if e.source_id != r.branch_id {
            return Err(conflict("snapshot export belongs to another source"));
        }
        self.db.execute_batch("SAVEPOINT sync_link")?;
        let result = (|| {
            r.refresh_id = Some(refresh);
            r.state = "running".into();
            self.save_sync_run(&r)?;
            self.mark_refresh(refresh)
        })();
        match result {
            Ok(()) => {
                self.db.execute_batch("RELEASE sync_link")?;
                Ok(())
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO sync_link; RELEASE sync_link")?;
                Err(e)
            }
        }
    }
    pub(crate) fn fail_sync_run(&self, id: OperationId, reason: &str, now: i64) -> Result<()> {
        let mut r = self
            .active_sync_runs()?
            .into_iter()
            .find(|r| r.id == id)
            .ok_or_else(|| missing("active snapshot run"))?;
        r.state = "failed".into();
        r.error = Some(reason.into());
        r.finished_at_ms = Some(now);
        self.save_sync_run(&r)?;
        let mut p = self.sync_policy(r.project_id, r.policy_id)?;
        p.error = r.error;
        if p.pause_requested {
            p.state = "paused".into();
            p.pause_requested = false;
        }
        self.save_sync_policy(&p)
    }
    pub(crate) fn reconcile_sync(&mut self, now: i64) -> Result<()> {
        self.db.execute("INSERT INTO analytics_gc SELECT e.id,NULL,'pending' FROM exports e JOIN sync_runs r ON r.refresh_id=e.id WHERE r.state='cancelled' AND e.state IN ('complete','failed','cancelled') AND NOT EXISTS(SELECT 1 FROM publications p WHERE p.export_id=e.id AND p.state='published') ON CONFLICT DO NOTHING", [])?;
        self.reconcile_triggered(now)?;
        // Repair admission crash gaps before cancellation or validation. No duplicate export.
        for r in self.active_sync_runs()? {
            if !r.config.incremental() && r.refresh_id.is_none() {
                let id: Option<String> = self
                    .db
                    .query_row(
                        "SELECT id FROM operations WHERE project_id=?1 AND request_key=?2",
                        params![r.project_id.to_string(), format!("internal:sync:{}", r.id)],
                        |row| row.get(0),
                    )
                    .optional()?;
                if let Some(id) = id {
                    self.link_sync_refresh(r.id, super::parse(&id)?)?;
                }
            }
        }
        // Observe publication first: cancellation after the atomic commit cannot undo success.
        for mut r in self.active_sync_runs()? {
            if let Some(id) = r.refresh_id {
                let status = self.refresh_status(r.project_id, id)?;
                match status["state"].as_str() {
                    Some("published") => {
                        r.state = "succeeded".into();
                        r.finished_at_ms = status["publication"]["published_at_ms"].as_i64();
                        r.epoch_id = status["publication"]["epoch_id"]
                            .as_str()
                            .map(str::to_owned);
                        r.source_lsn =
                            status["publication"]["descriptor"]["manifest"]["source"]["lsn"]
                                .as_str()
                                .map(str::to_owned);
                        let mut p = self.sync_policy(r.project_id, r.policy_id)?;
                        p.last_success_at_ms = r.finished_at_ms;
                        p.last_epoch_id = r.epoch_id.clone();
                        p.error = None;
                        self.db.execute_batch("SAVEPOINT sync_success")?;
                        let result = (|| {
                            self.save_sync_run(&r)?;
                            self.save_sync_policy(&p)
                        })();
                        match result {
                            Ok(()) => self.db.execute_batch("RELEASE sync_success")?,
                            Err(e) => {
                                self.db.execute_batch(
                                    "ROLLBACK TO sync_success; RELEASE sync_success",
                                )?;
                                return Err(e);
                            }
                        }
                    }
                    Some("failed" | "cancelled") => {
                        self.fail_sync_run(r.id, "snapshot_refresh_failed", now)?
                    }
                    _ => (),
                }
            }
        }
        for mut p in self
            .sync_policies()?
            .into_iter()
            .filter(|p| matches!(p.state.as_str(), "active" | "paused"))
        {
            if self.sync_source(&p).is_err() {
                self.db.execute_batch("SAVEPOINT sync_block")?;
                let result = (|| {
                    for r in self
                        .active_sync_runs()?
                        .into_iter()
                        .filter(|r| r.policy_id == p.id)
                    {
                        self.cancel_sync_run(r, "source_unavailable", now)?;
                    }
                    if p.state != "blocked" {
                        p.revision += 1;
                    }
                    p.state = "blocked".into();
                    p.error = Some("source_unavailable_or_authority_changed".into());
                    p.next_due_at_ms = None;
                    self.save_sync_policy(&p)
                })();
                match result {
                    Ok(()) => self.db.execute_batch("RELEASE sync_block")?,
                    Err(e) => {
                        self.db
                            .execute_batch("ROLLBACK TO sync_block; RELEASE sync_block")?;
                        return Err(e);
                    }
                }
            }
        }
        Ok(())
    }
    pub(crate) fn sync_publication_live(&self, id: OperationId) -> Result<()> {
        // A run can be unlinked only in the crash window after durable export admission.
        // Find it by the durable internal request key as well as the materialized FK.
        let record: Option<String> = self.db.query_row("SELECT r.record FROM sync_runs r JOIN operations o ON o.id=?1 WHERE r.refresh_id=o.id OR o.request_key='internal:sync:' || r.id",[id.to_string()],|r|r.get(0)).optional()?;
        if let Some(text) = record {
            let r: Run = serde_json::from_str(&text)?;
            let p = self.sync_policy(r.project_id, r.policy_id)?;
            if p.state != "active"
                || p.revision != r.policy_revision
                || !matches!(r.state.as_str(), "starting" | "running")
            {
                return Err(conflict("snapshot policy/run is fenced"));
            }
            self.sync_source(&p)?;
        }
        Ok(())
    }
}

/// Called only on a verified stopped backup copy, before any daemon can execute.
pub(crate) fn restore(db: &rusqlite::Connection, now: i64) -> Result<()> {
    let tx = db.unchecked_transaction()?;
    triggered::restore_success(&tx)?;
    // A stopped backup can land after epoch commit but before run reconciliation.
    // Preserve that success before cancelling copied unfinished intent.
    let published = tx.prepare("SELECT r.record,p.epoch_id,p.published_at_ms,p.descriptor FROM sync_runs r JOIN publications p ON p.export_id=r.refresh_id WHERE r.state IN ('starting','running') AND p.state='published'")?
        .query_map([], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?,r.get::<_,String>(3)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
    for (record, epoch, at, descriptor) in published {
        let mut run: Run = serde_json::from_str(&record)?;
        let descriptor: Value = serde_json::from_str(&descriptor)?;
        run.state = "succeeded".into();
        run.epoch_id = Some(epoch.clone());
        run.finished_at_ms = Some(at);
        run.source_lsn = descriptor["manifest"]["source"]["lsn"]
            .as_str()
            .map(str::to_owned);
        tx.execute(
            "UPDATE sync_runs SET state='succeeded',record=?2 WHERE id=?1",
            params![run.id.to_string(), serde_json::to_string(&run)?],
        )?;
        tx.execute("UPDATE sync_policies SET record=json_set(record,'$.last_success_at_ms',?2,'$.last_epoch_id',?3) WHERE id=?1",params![run.policy_id.to_string(),at,epoch])?;
    }
    // Repair the same admission gap as startup, then fence every copied pending run.
    tx.execute_batch("UPDATE sync_runs SET refresh_id=(SELECT o.id FROM operations o WHERE o.project_id=sync_runs.project_id AND o.request_key='internal:sync:' || sync_runs.id) WHERE refresh_id IS NULL AND state IN ('queued','starting','running');
        UPDATE exports SET cancel_requested=1 WHERE id IN (SELECT refresh_id FROM sync_runs WHERE state IN ('queued','starting','running'));
        INSERT INTO analytical_refreshes(export_id,error) SELECT refresh_id,'cancelled' FROM sync_runs WHERE refresh_id IS NOT NULL AND state IN ('queued','starting','running') ON CONFLICT(export_id) DO UPDATE SET error='cancelled';
        UPDATE publications SET state='cancelled',error='restored_requires_resume' WHERE export_id IN (SELECT refresh_id FROM sync_runs WHERE state IN ('queued','starting','running')) AND state IN ('requested','files_complete');
        UPDATE sync_policies SET state='paused',record=json_set(record,'$.state','paused','$.pause_requested',json('false'),'$.revision',json_extract(record,'$.revision')+1,'$.next_due_at_ms',NULL,'$.error','restored_requires_resume') WHERE state!='deleted';")?;
    tx.execute("UPDATE sync_runs SET state='cancelled',record=json_set(record,'$.state','cancelled','$.refresh_id',refresh_id,'$.finished_at_ms',?1,'$.error','restored_requires_resume') WHERE state IN ('queued','starting','running')",[now])?;
    tx.commit()?;
    Ok(())
}

mod continuous;
pub(crate) mod governed;
mod surfaces;
mod triggered;

#[cfg(test)]
mod tests;
