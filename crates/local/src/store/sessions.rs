use super::{
    Result, Store,
    error::{conflict, invalid, missing},
    now_ms,
};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use supabricks_core::resource::{BranchId, EpochId, OperationId, ProjectId};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AnalyticalSession {
    pub id: OperationId,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub epoch_id: Option<EpochId>,
    pub refresh_id: Option<OperationId>,
    pub state: String,
    pub created_at_ms: i64,
    pub expires_at_ms: i64,
    pub endpoint: Option<String>,
    pub metadata: Option<Value>,
    pub error: Option<String>,
    /// Only the latest bounded query result is retained per session.
    pub query: Option<Value>,
}
impl Store {
    pub fn analytical_session(
        &self,
        project: ProjectId,
        id: OperationId,
    ) -> Result<AnalyticalSession> {
        let text: String = self
            .db
            .query_row(
                "SELECT record FROM analytical_sessions WHERE id=?1 AND project_id=?2",
                params![id.to_string(), project.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("analytical session in project"))?;
        Ok(serde_json::from_str(&text)?)
    }
    pub fn session_for_key(
        &self,
        project: ProjectId,
        key: &str,
        request: &Value,
    ) -> Result<Option<AnalyticalSession>> {
        if key.is_empty() || key.len() > 256 {
            return Err(invalid("session key requires 1–256 bytes"));
        }
        let row: Option<(String,String)> = self.db.query_row("SELECT request,record FROM analytical_sessions WHERE project_id=?1 AND request_key=?2",params![project.to_string(),key],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        match row {
            Some((old, record)) => {
                if serde_json::from_str::<Value>(&old)? != *request {
                    return Err(conflict(
                        "session key was already used with different parameters",
                    ));
                }
                Ok(Some(serde_json::from_str(&record)?))
            }
            None => Ok(None),
        }
    }
    pub fn active_analytical_sessions(&self) -> Result<Vec<AnalyticalSession>> {
        let rows=self.db.prepare("SELECT record FROM analytical_sessions WHERE state IN ('waiting','starting','ready','closing') ORDER BY created_at_ms")?.query_map([],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        rows.iter().map(|s| Ok(serde_json::from_str(s)?)).collect()
    }
    pub fn admit_analytical_session(
        &mut self,
        project: ProjectId,
        branch: BranchId,
        key: &str,
        request: Value,
        epoch: Option<EpochId>,
        refresh: Option<OperationId>,
        ttl_ms: u64,
    ) -> Result<AnalyticalSession> {
        if let Some(s) = self.session_for_key(project, key, &request)? {
            return Ok(s);
        }
        if !(10_000..=3_600_000).contains(&ttl_ms) {
            return Err(invalid("session lifetime requires 10–3600 seconds"));
        }
        self.branch_in_project(project, branch)?;
        if self.active_analytical_sessions()?.len() >= 2 {
            return Err(conflict(
                "both analytical session slots are occupied; close a session first",
            ));
        }
        if let Some(id) = epoch {
            let snapshot = self.snapshot(project, id)?;
            if snapshot.state != "available" || snapshot.publication.branch_id != branch {
                return Err(conflict(
                    "selected epoch is unavailable or belongs to another branch",
                ));
            }
        } else if let Some(id) = refresh {
            if self.export_in_project(project, id)?.source_id != branch {
                return Err(conflict("refresh belongs to another branch"));
            }
        } else {
            return Err(invalid("session requires an epoch or refresh"));
        }
        let now = now_ms()?;
        let s = AnalyticalSession {
            id: OperationId::new(),
            project_id: project,
            branch_id: branch,
            epoch_id: epoch,
            refresh_id: refresh,
            state: if epoch.is_some() {
                "starting"
            } else {
                "waiting"
            }
            .into(),
            created_at_ms: now,
            expires_at_ms: now + ttl_ms as i64,
            endpoint: None,
            metadata: None,
            error: None,
            query: None,
        };
        self.db.execute(
            "INSERT INTO analytical_sessions VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
            params![
                s.id.to_string(),
                project.to_string(),
                branch.to_string(),
                key,
                request.to_string(),
                epoch.map(|e| e.to_string()),
                refresh.map(|e| e.to_string()),
                s.state,
                now,
                s.expires_at_ms,
                serde_json::to_string(&s)?
            ],
        )?;
        Ok(s)
    }
    pub(crate) fn save_analytical_session(&mut self, s: &AnalyticalSession) -> Result<()> {
        self.db.execute(
            "UPDATE analytical_sessions SET epoch_id=?2,state=?3,record=?4 WHERE id=?1",
            params![
                s.id.to_string(),
                s.epoch_id.map(|e| e.to_string()),
                s.state,
                serde_json::to_string(s)?
            ],
        )?;
        Ok(())
    }
    pub(crate) fn bind_session_epoch(
        &mut self,
        s: &mut AnalyticalSession,
        epoch: EpochId,
    ) -> Result<()> {
        let snapshot = self.snapshot(s.project_id, epoch)?;
        if snapshot.state != "available" || snapshot.publication.branch_id != s.branch_id {
            return Err(conflict("refresh snapshot is no longer available"));
        }
        s.epoch_id = Some(epoch);
        s.state = "starting".into();
        self.save_analytical_session(s)
    }
    pub fn close_analytical_session(
        &mut self,
        project: ProjectId,
        id: OperationId,
        reason: &str,
    ) -> Result<AnalyticalSession> {
        let mut s = self.analytical_session(project, id)?;
        if matches!(s.state.as_str(), "waiting" | "starting" | "ready") {
            s.state = "closing".into();
            s.error = Some(reason.into());
            s.endpoint = None;
            if let Some(q) = s.query.as_mut() {
                if q["state"] == "running" {
                    q["state"] = json!("cancelled");
                    q["error"] = json!(reason);
                }
            }
            self.save_analytical_session(&s)?;
        }
        Ok(s)
    }
    pub(crate) fn mark_refresh(&mut self, id: OperationId) -> Result<()> {
        self.db.execute(
            "INSERT INTO analytical_refreshes(export_id) VALUES (?1) ON CONFLICT DO NOTHING",
            [id.to_string()],
        )?;
        Ok(())
    }
    pub(crate) fn pending_refreshes(&self) -> Result<Vec<(ProjectId, OperationId)>> {
        let rows=self.db.prepare("SELECT e.project_id,e.id FROM analytical_refreshes r JOIN exports e ON e.id=r.export_id LEFT JOIN publications p ON p.export_id=e.id WHERE r.error IS NULL AND e.state NOT IN ('failed','cancelled') AND (p.state IS NULL OR p.state IN ('requested','files_complete'))")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
        rows.into_iter()
            .map(|(p, id)| Ok((super::parse(&p)?, super::parse(&id)?)))
            .collect()
    }
    pub(crate) fn fail_refresh(&mut self, id: OperationId, error: &str) -> Result<()> {
        self.db.execute(
            "UPDATE analytical_refreshes SET error=?2 WHERE export_id=?1",
            params![id.to_string(), error],
        )?;
        Ok(())
    }
    pub fn refresh_status(&self, project: ProjectId, id: OperationId) -> Result<Value> {
        let export = self.export_in_project(project, id)?;
        let publication = match self.publication_in_project(project, id) {
            Ok(p) => Some(p),
            Err(super::Error::Operation(supabricks_core::error::OperationError::NotFound(_))) => {
                None
            }
            Err(e) => return Err(e),
        };
        let error: Option<String> = self
            .db
            .query_row(
                "SELECT error FROM analytical_refreshes WHERE export_id=?1",
                [id.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .flatten();
        let state = publication.as_ref().map(|p| p.state.as_str()).unwrap_or(
            if export.state == "complete" {
                "awaiting_publication"
            } else {
                export.state.as_str()
            },
        );
        Ok(
            json!({"id":id,"state":if error.is_some(){"failed"}else{state},"error":error,"export":export,"publication":publication}),
        )
    }
}
