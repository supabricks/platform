//! Atomic publication metadata and durable analytical-reader leases.
use super::{
    Result, Store,
    error::{conflict, invalid, missing},
    now_ms, parse,
};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use supabricks_core::resource::*;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Publication {
    pub ordinal: i64,
    pub export_id: OperationId,
    pub epoch_id: EpochId,
    pub branch_id: BranchId,
    pub source_revision: i64,
    pub export_order: i64,
    pub requested_at_ms: i64,
    pub published_at_ms: Option<i64>,
    pub state: String,
    pub descriptor: Option<Value>,
    pub error: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Snapshot {
    pub publication: Publication,
    pub state: String,
    pub error: Option<String>,
    pub current: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SnapshotLease {
    pub id: LeaseId,
    pub epoch_id: EpochId,
    pub expires_at_ms: i64,
}
impl Store {
    pub fn installation_id(&self) -> Result<String> {
        Ok(self
            .db
            .query_row("SELECT id FROM analytics_installation", [], |r| r.get(0))?)
    }
    pub fn publication(&self, id: OperationId) -> Result<Publication> {
        self.publication_record(id, true)
    }
    fn publication_record(&self, id: OperationId, descriptor: bool) -> Result<Publication> {
        let r=self.db.query_row("SELECT ordinal,epoch_id,branch_id,source_revision,export_order,requested_at_ms,published_at_ms,state,CASE WHEN ?2 THEN descriptor ELSE NULL END,error FROM publications WHERE export_id=?1",params![id.to_string(),descriptor],|r|Ok((r.get(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get::<_,Option<String>>(8)?,r.get(9)?))).optional()?.ok_or_else(||missing("publication"))?;
        Ok(Publication {
            ordinal: r.0,
            export_id: id,
            epoch_id: parse(&r.1)?,
            branch_id: parse(&r.2)?,
            source_revision: r.3,
            export_order: r.4,
            requested_at_ms: r.5,
            published_at_ms: r.6,
            state: r.7,
            descriptor: r.8.map(|v| serde_json::from_str(&v)).transpose()?,
            error: r.9,
        })
    }
    pub fn publication_in_project(
        &self,
        project: ProjectId,
        id: OperationId,
    ) -> Result<Publication> {
        self.export_in_project(project, id)?;
        self.publication(id)
    }
    pub fn publish_export(&mut self, project: ProjectId, id: OperationId) -> Result<Publication> {
        let e = self.export_in_project(project, id)?;
        if self
            .db
            .prepare("SELECT 1 FROM publications WHERE export_id=?1")?
            .exists([id.to_string()])?
        {
            return self.publication(id);
        }
        let b = self.branch(e.source_id)?;
        if e.state != "complete"
            || e.outcome
                .as_ref()
                .is_none_or(|v| v["status"] != "files_complete")
            || b.endpoint.desired_state == DesiredState::Deleted
            || b.expired
        {
            return Err(conflict(
                "publish requires a completed export from a live source branch",
            ));
        }
        let tx = self.db.transaction()?;
        if tx
            .prepare("SELECT 1 FROM analytics_gc WHERE export_id=?1")?
            .exists([id.to_string()])?
        {
            return Err(conflict("export has been discarded"));
        }
        if tx.prepare("SELECT 1 FROM publications WHERE branch_id=?1 AND state IN ('requested','files_complete')")?.exists([e.source_id.to_string()])? {return Err(conflict("one publication per branch may be in flight"));}
        let order: i64 = tx.query_row(
            "SELECT rowid FROM operations WHERE id=?1",
            [id.to_string()],
            |r| r.get(0),
        )?;
        let old:Option<i64>=tx.query_row("SELECT p.export_order FROM snapshot_heads h JOIN publications p ON p.epoch_id=h.epoch_id WHERE h.branch_id=?1",[e.source_id.to_string()],|r|r.get(0)).optional()?;
        if old.is_some_and(|o| o >= order) {
            return Err(conflict(
                "an equal or newer export has already been published",
            ));
        }
        tx.execute("INSERT INTO publications(export_id,epoch_id,branch_id,source_revision,export_order,requested_at_ms,state) VALUES (?1,?2,?3,?4,?5,?6,'requested')",params![id.to_string(),EpochId::new().to_string(),e.source_id.to_string(),b.revision,order,now_ms()?])?;
        tx.commit()?;
        self.publication(id)
    }
    pub(crate) fn pending_publications(&self) -> Result<Vec<Publication>> {
        let ids=self.db.prepare("SELECT export_id FROM publications WHERE state IN ('requested','files_complete') ORDER BY ordinal")?.query_map([],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        ids.iter().map(|id| self.publication(parse(id)?)).collect()
    }
    pub(crate) fn publication_ready(&mut self, p: &Publication, descriptor: &Value) -> Result<()> {
        self.db.execute("UPDATE publications SET state='files_complete',descriptor=?2 WHERE export_id=?1 AND state='requested'",params![p.export_id.to_string(),descriptor.to_string()])?;
        Ok(())
    }
    pub(crate) fn fail_publication(&mut self, id: OperationId, error: &str) -> Result<()> {
        let tx = self.db.transaction()?;
        let n=tx.execute("UPDATE publications SET state='failed',error=?2 WHERE export_id=?1 AND state IN ('requested','files_complete')",params![id.to_string(),error])?;
        if n == 1 {
            tx.execute(
                "INSERT INTO analytics_gc VALUES (?1,NULL,'pending') ON CONFLICT DO NOTHING",
                [id.to_string()],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn discard_export(&mut self, project: ProjectId, id: OperationId) -> Result<Value> {
        let e = self.export_in_project(project, id)?;
        if !matches!(e.state.as_str(), "complete" | "failed" | "cancelled") {
            return Err(conflict(
                "cancel the active export and wait for cleanup first",
            ));
        }
        let tx = self.db.transaction()?;
        if tx
            .prepare("SELECT 1 FROM publications WHERE export_id=?1 AND state='published'")?
            .exists([id.to_string()])?
        {
            return Err(conflict(
                "published snapshots use retention GC, not discard",
            ));
        }
        tx.execute("UPDATE publications SET state='cancelled' WHERE export_id=?1 AND state IN ('requested','files_complete')",[id.to_string()])?;
        tx.execute(
            "INSERT INTO analytics_gc VALUES (?1,NULL,'pending') ON CONFLICT DO NOTHING",
            [id.to_string()],
        )?;
        tx.commit()?;
        Ok(json!({"export_id":id,"discard_requested":true}))
    }
    /// All table mappings, the immutable descriptor and the branch pointer
    /// become visible in this single FULL-synchronous SQLite transaction.
    pub(crate) fn commit_publication(&mut self, p: &Publication) -> Result<()> {
        let p = self.publication(p.export_id)?;
        if p.state == "published" {
            return Ok(());
        }
        if p.state != "files_complete" {
            return Err(conflict("publication is not ready"));
        }
        let b = self.branch(p.branch_id)?;
        if b.revision != p.source_revision
            || b.expired
            || b.endpoint.desired_state == DesiredState::Deleted
        {
            return Err(conflict("source revision changed during publication"));
        }
        let d = p
            .descriptor
            .as_ref()
            .ok_or_else(|| conflict("missing snapshot descriptor"))?;
        let tx = self.db.transaction()?;
        let old:Option<i64>=tx.query_row("SELECT p.export_order FROM snapshot_heads h JOIN publications p ON p.epoch_id=h.epoch_id WHERE h.branch_id=?1",[p.branch_id.to_string()],|r|r.get(0)).optional()?;
        if old.is_some_and(|o| o >= p.export_order) {
            return Err(conflict(
                "stale publication cannot replace a newer snapshot",
            ));
        }
        tx.execute(
            "INSERT INTO epochs VALUES (?1,?2,?3)",
            params![
                p.epoch_id.to_string(),
                p.branch_id.to_string(),
                d["manifest"]["source"]["lsn"]
                    .as_str()
                    .ok_or_else(|| invalid("missing source LSN"))?
            ],
        )?;
        for table in d["manifest"]["tables"]
            .as_array()
            .ok_or_else(|| invalid("missing tables"))?
        {
            tx.execute(
                "INSERT INTO table_mappings VALUES (?1,?2,?3,?4)",
                params![
                    p.epoch_id.to_string(),
                    table["oid"]
                        .as_u64()
                        .ok_or_else(|| invalid("missing OID"))? as i64,
                    json!([table["schema"], table["name"]]).to_string(),
                    format!(
                        "analytics/generations/{}/{}",
                        p.export_id,
                        table["path"]
                            .as_str()
                            .ok_or_else(|| invalid("missing table path"))?
                    )
                ],
            )?;
        }
        tx.execute(
            "INSERT INTO snapshots VALUES (?1,?2,'available',NULL)",
            params![p.epoch_id.to_string(), p.export_id.to_string()],
        )?;
        tx.execute("INSERT INTO snapshot_heads VALUES (?1,?2) ON CONFLICT(branch_id) DO UPDATE SET epoch_id=excluded.epoch_id",params![p.branch_id.to_string(),p.epoch_id.to_string()])?;
        tx.execute(
            "UPDATE publications SET state='published',published_at_ms=?2 WHERE export_id=?1",
            params![p.export_id.to_string(), now_ms()?],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn snapshot(&self, project: ProjectId, id: EpochId) -> Result<Snapshot> {
        self.snapshot_record(project, id, true)
    }
    fn snapshot_record(
        &self,
        project: ProjectId,
        id: EpochId,
        descriptor: bool,
    ) -> Result<Snapshot> {
        let r=self.db.query_row("SELECT s.export_id,s.state,s.error,EXISTS(SELECT 1 FROM snapshot_heads h WHERE h.epoch_id=s.epoch_id) FROM snapshots s JOIN epochs e ON e.id=s.epoch_id JOIN branches b ON b.id=e.branch_id WHERE s.epoch_id=?1 AND b.project_id=?2",params![id.to_string(),project.to_string()],|r|Ok((r.get::<_,String>(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?.ok_or_else(||missing("snapshot in project"))?;
        Ok(Snapshot {
            publication: self.publication_record(parse(&r.0)?, descriptor)?,
            state: r.1,
            error: r.2,
            current: r.3,
        })
    }
    pub fn current_snapshot(&self, project: ProjectId, branch: BranchId) -> Result<Snapshot> {
        self.branch_in_project(project, branch)?;
        let id: String = self
            .db
            .query_row(
                "SELECT epoch_id FROM snapshot_heads WHERE branch_id=?1",
                [branch.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("published snapshot"))?;
        let s = self.snapshot(project, parse(&id)?)?;
        if s.state != "available" {
            return Err(conflict(
                "current snapshot is unavailable; inspect snapshot history and restore its files",
            ));
        }
        Ok(s)
    }
    pub fn snapshots(&self, project: ProjectId, branch: BranchId) -> Result<Vec<Snapshot>> {
        self.snapshot_history(project, branch, None, 100)
    }
    pub fn snapshot_history(
        &self,
        project: ProjectId,
        branch: BranchId,
        before: Option<i64>,
        limit: usize,
    ) -> Result<Vec<Snapshot>> {
        self.branch_in_project(project, branch)?;
        if !(1..=100).contains(&limit) {
            return Err(invalid("snapshot history limit must be 1–100"));
        }
        let ids=self.db.prepare("SELECT s.epoch_id FROM snapshots s JOIN publications p ON p.export_id=s.export_id WHERE p.branch_id=?1 AND p.ordinal<?2 ORDER BY p.ordinal DESC LIMIT ?3")?
            .query_map(params![branch.to_string(),before.unwrap_or(i64::MAX),limit as i64],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        ids.iter()
            .map(|id| self.snapshot_record(project, parse(id)?, false))
            .collect()
    }
    pub(crate) fn recoverable_snapshot_ids(&self) -> Result<Vec<(ProjectId, EpochId)>> {
        let rows=self.db.prepare("SELECT b.project_id,s.epoch_id FROM snapshots s JOIN epochs e ON e.id=s.epoch_id JOIN branches b ON b.id=e.branch_id WHERE s.state IN ('available','unavailable')")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
        rows.iter()
            .map(|(project, id)| Ok((parse(project)?, parse(id)?)))
            .collect()
    }
    pub(crate) fn restored_snapshot(&mut self, id: EpochId) -> Result<()> {
        self.db.execute("UPDATE snapshots SET state='available',error=NULL WHERE epoch_id=?1 AND state='unavailable'",[id.to_string()])?;
        Ok(())
    }
    pub(crate) fn unavailable_snapshot(&mut self, id: EpochId, error: &str) -> Result<()> {
        self.db.execute("UPDATE snapshots SET state='unavailable',error=?2 WHERE epoch_id=?1 AND state='available'",params![id.to_string(),error])?;
        Ok(())
    }
    pub fn pin_snapshot(
        &mut self,
        project: ProjectId,
        id: EpochId,
        ttl_ms: u64,
    ) -> Result<SnapshotLease> {
        if self.snapshot(project, id)?.state != "available" {
            return Err(conflict("snapshot is not available for new readers"));
        }
        self.db.execute(
            "DELETE FROM snapshot_leases WHERE expires_at_ms<=?1",
            [now_ms()?],
        )?;
        let count: i64 = self
            .db
            .query_row("SELECT count(*) FROM snapshot_leases", [], |r| r.get(0))?;
        if count >= 1024 {
            return Err(conflict("installation snapshot lease limit reached"));
        }
        let l = SnapshotLease {
            id: LeaseId::new(),
            epoch_id: id,
            expires_at_ms: lease_expiry(ttl_ms)?,
        };
        self.db.execute(
            "INSERT INTO snapshot_leases VALUES (?1,?2,?3)",
            params![l.id.to_string(), id.to_string(), l.expires_at_ms],
        )?;
        Ok(l)
    }
    pub fn renew_snapshot_lease(
        &mut self,
        project: ProjectId,
        id: LeaseId,
        ttl_ms: u64,
    ) -> Result<SnapshotLease> {
        let epoch: String = self
            .db
            .query_row(
                "SELECT epoch_id FROM snapshot_leases WHERE id=?1 AND expires_at_ms>?2",
                params![id.to_string(), now_ms()?],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("live snapshot lease"))?;
        let epoch_id = parse(&epoch)?;
        if self.snapshot(project, epoch_id)?.state != "available" {
            return Err(conflict("snapshot is unavailable"));
        }
        let expires = lease_expiry(ttl_ms)?;
        self.db.execute(
            "UPDATE snapshot_leases SET expires_at_ms=?2 WHERE id=?1",
            params![id.to_string(), expires],
        )?;
        Ok(SnapshotLease {
            id,
            epoch_id,
            expires_at_ms: expires,
        })
    }
    pub fn release_snapshot_lease(&mut self, project: ProjectId, id: LeaseId) -> Result<()> {
        let epoch: Option<String> = self
            .db
            .query_row(
                "SELECT epoch_id FROM snapshot_leases WHERE id=?1",
                [id.to_string()],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(epoch) = epoch {
            self.snapshot(project, parse(&epoch)?)?;
            self.db
                .execute("DELETE FROM snapshot_leases WHERE id=?1", [id.to_string()])?;
        }
        Ok(())
    }
    /// Mark before unlink: the same writer admits leases, so a deleting epoch
    /// cannot acquire a new reader after the reference check.
    pub fn collect_snapshots(
        &mut self,
        project: ProjectId,
        branch: BranchId,
        keep: usize,
    ) -> Result<Value> {
        if !(1..=100).contains(&keep) {
            return Err(invalid("retain 1–100 snapshots, including the current one"));
        }
        self.branch_in_project(project, branch)?;
        let tx = self.db.transaction()?;
        let rows=tx.prepare("WITH ranked AS (
          SELECT s.epoch_id,s.export_id,row_number() OVER (ORDER BY p.ordinal DESC) AS rank
          FROM snapshots s JOIN publications p ON p.export_id=s.export_id
          WHERE p.branch_id=?1 AND s.state IN ('available','unavailable'))
          SELECT epoch_id,export_id FROM ranked r WHERE rank>?2
          AND NOT EXISTS(SELECT 1 FROM snapshot_heads h WHERE h.epoch_id=r.epoch_id)
          AND NOT EXISTS(SELECT 1 FROM snapshot_leases l WHERE l.epoch_id=r.epoch_id AND expires_at_ms>?3)
          AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.epoch_id=r.epoch_id AND expires_at_ms>?3)
          AND NOT EXISTS(SELECT 1 FROM analytical_sessions a WHERE a.epoch_id=r.epoch_id AND a.state IN ('waiting','starting','ready','closing'))
          AND NOT EXISTS(SELECT 1 FROM analytical_sessions a JOIN publications p ON p.export_id=a.refresh_id WHERE p.epoch_id=r.epoch_id AND a.state='waiting')
          LIMIT 256")?.query_map(params![branch.to_string(),keep as i64,now_ms()?],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
        let mut selected: Vec<EpochId> = Vec::new();
        for (id, export) in rows {
            tx.execute(
                "UPDATE snapshots SET state='deleting' WHERE epoch_id=?1",
                [&id],
            )?;
            tx.execute(
                "INSERT INTO analytics_gc VALUES (?1,?2,'pending') ON CONFLICT DO NOTHING",
                params![export, id],
            )?;
            selected.push(parse(&id)?);
        }
        tx.commit()?;
        Ok(json!({"deleting":selected,"keep":keep}))
    }
    pub(crate) fn pending_analytics_gc(&self) -> Result<Vec<OperationId>> {
        self.db
            .prepare("SELECT export_id FROM analytics_gc WHERE state='pending'")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?
            .iter()
            .map(|s| parse(s))
            .collect()
    }
    pub(crate) fn finish_analytics_gc(&mut self, id: OperationId) -> Result<()> {
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE snapshots SET state='deleted' WHERE export_id=?1 AND state='deleting'",
            [id.to_string()],
        )?;
        tx.execute(
            "UPDATE analytics_gc SET state='done' WHERE export_id=?1",
            [id.to_string()],
        )?;
        tx.commit()?;
        Ok(())
    }
}
fn lease_expiry(ttl_ms: u64) -> Result<i64> {
    if !(1000..=86_400_000).contains(&ttl_ms) {
        return Err(invalid("snapshot lease requires 1–86400 seconds"));
    }
    Ok(now_ms()? + ttl_ms as i64)
}
