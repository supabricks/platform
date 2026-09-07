//! Unpublished A01 generations. Publication and epoch retention are A02.
use super::{
    Result, Store,
    error::{conflict, invalid, missing},
    now_ms, parse,
};
use crate::operations::Mutation;
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use supabricks_core::resource::{BranchId, OperationId, ProjectId};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportLimits {
    pub max_bytes: u64,
    pub timeout_ms: u64,
}
impl Default for ExportLimits {
    fn default() -> Self {
        Self {
            max_bytes: 1024 * 1024 * 1024,
            timeout_ms: 300_000,
        }
    }
}
impl ExportLimits {
    pub fn validate(&self) -> Result<()> {
        if !(16 * 1024 * 1024..=16 * 1024 * 1024 * 1024).contains(&self.max_bytes)
            || !(10_000..=1_800_000).contains(&self.timeout_ms)
        {
            return Err(invalid(
                "export requires 16 MiB–16 GiB output budget and 10–1800 seconds",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ExportRecord {
    pub id: OperationId,
    pub project_id: ProjectId,
    pub source_id: BranchId,
    pub child_id: BranchId,
    pub limits: ExportLimits,
    pub deadline_ms: i64,
    pub state: String,
    pub cancel_requested: bool,
    pub outcome: Option<Value>,
    pub cleanup_id: Option<OperationId>,
}
impl Store {
    pub fn is_export(&self, branch: BranchId) -> Result<bool> {
        Ok(self
            .db
            .prepare("SELECT 1 FROM exports WHERE child_id=?1")?
            .exists([branch.to_string()])?)
    }
    pub fn export(&self, id: OperationId) -> Result<ExportRecord> {
        let row = self.db.query_row("SELECT project_id,source_id,child_id,limits,deadline_ms,state,cancel_requested,outcome,cleanup_id FROM exports WHERE id=?1", [id.to_string()], |r| Ok((
            r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?,
            r.get(4)?, r.get(5)?, r.get(6)?, r.get::<_, Option<String>>(7)?, r.get::<_, Option<String>>(8)?
        ))).optional()?.ok_or_else(|| missing("export"))?;
        Ok(ExportRecord {
            id,
            project_id: parse(&row.0)?,
            source_id: parse(&row.1)?,
            child_id: parse(&row.2)?,
            limits: serde_json::from_str(&row.3)?,
            deadline_ms: row.4,
            state: row.5,
            cancel_requested: row.6,
            outcome: row.7.map(|s| serde_json::from_str(&s)).transpose()?,
            cleanup_id: row.8.map(|s| parse(&s)).transpose()?,
        })
    }
    pub fn export_in_project(&self, project: ProjectId, id: OperationId) -> Result<ExportRecord> {
        let e = self.export(id)?;
        if e.project_id != project {
            return Err(missing("export in project"));
        }
        Ok(e)
    }
    pub fn active_exports(&self) -> Result<Vec<ExportRecord>> {
        let ids = self.db.prepare("SELECT id FROM exports WHERE state IN ('preparing','exporting','cleaning') ORDER BY id")?
            .query_map([], |r| r.get::<_, String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        ids.iter().map(|id| self.export(parse(id)?)).collect()
    }
    pub fn cancel_export(&mut self, project: ProjectId, id: OperationId) -> Result<ExportRecord> {
        let e = self.export_in_project(project, id)?;
        if e.state == "complete" {
            return Err(conflict("completed export cannot be cancelled"));
        }
        self.db.execute("UPDATE exports SET cancel_requested=1 WHERE id=?1 AND state IN ('preparing','exporting')", [id.to_string()])?;
        self.export(id)
    }
    pub(crate) fn export_started(&mut self, id: OperationId) -> Result<()> {
        self.db.execute(
            "UPDATE exports SET state='exporting' WHERE id=?1 AND state='preparing'",
            [id.to_string()],
        )?;
        Ok(())
    }
    pub(crate) fn export_outcome(&mut self, id: OperationId, result: Value) -> Result<()> {
        self.db.execute("UPDATE exports SET state='cleaning',outcome=?2 WHERE id=?1 AND state IN ('preparing','exporting')", params![id.to_string(), result.to_string()])?;
        Ok(())
    }
    pub(crate) fn interrupt_exports(&mut self) -> Result<()> {
        for e in self.active_exports()? {
            if e.state == "exporting" {
                self.export_outcome(e.id, json!({"status":"failed","code":"worker_interrupted"}))?;
            }
        }
        Ok(())
    }
    pub(crate) fn cleanup_export(&mut self, e: &ExportRecord) -> Result<()> {
        if e.state != "cleaning" {
            return Err(conflict("export cleanup is not authorized"));
        }
        let b = self.branch(e.child_id)?;
        let key = format!("internal:export-cleanup:{}", e.id);
        let request = self
            .request_for_key(e.project_id, &key)?
            .unwrap_or(Mutation::ForceDelete {
                branch_id: e.child_id,
                expected_revision: b.revision,
            });
        let op = self.submit_impl(e.project_id, &key, request, true)?;
        self.db.execute(
            "UPDATE exports SET cleanup_id=?2 WHERE id=?1",
            params![e.id.to_string(), op.id.to_string()],
        )?;
        Ok(())
    }
    pub(crate) fn finish_export(&mut self, e: &ExportRecord) -> Result<()> {
        let status = e
            .outcome
            .as_ref()
            .and_then(|v| v["status"].as_str())
            .unwrap_or("failed");
        let state = if status == "files_complete" {
            "complete"
        } else if status == "cancelled" {
            "cancelled"
        } else {
            "failed"
        };
        self.db.execute(
            "UPDATE exports SET state=?2 WHERE id=?1 AND state='cleaning'",
            params![e.id.to_string(), state],
        )?;
        Ok(())
    }
    pub(crate) fn renew_export_lease(&mut self, e: &ExportRecord) -> Result<()> {
        self.db.execute("UPDATE leases SET generation=?2,expires_at_ms=?3 WHERE id=(SELECT lease_id FROM exports WHERE id=?1)",
            params![e.id.to_string(),self.generation(),now_ms()? + 60_000])?;
        Ok(())
    }
}
