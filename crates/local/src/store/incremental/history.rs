//! Bounded recent execution history, independent of retained analytical data.
use super::*;

const HIGH_WATER: i64 = 768;
const KEEP_RECENT: i64 = 512;
const DELETE_BATCH: i64 = 128;

impl Store {
    /// Only successful private batches owned by completed managed runs expire.
    /// Publications, snapshots, catalog references, cursors and audit are untouched.
    /// Public retry receipts and failed/cancelled work remain under their existing
    /// budgets; exhaustion with no eligible history continues to fail closed.
    pub(crate) fn retain_incremental_history(&self) -> Result<()> {
        self.db.execute_batch("SAVEPOINT incremental_history")?;
        let result = (|| {
            let count: i64 =
                self.db
                    .query_row("SELECT count(*) FROM incremental_runs", [], |r| r.get(0))?;
            if count >= HIGH_WATER {
                let ids = self.db.prepare(
                    "SELECT r.id FROM incremental_runs r
                     JOIN sync_runs owner ON owner.id=json_extract(r.record,'$.sync_run_id')
                     JOIN analytical_artifacts a ON a.id=r.id
                     JOIN publications pub ON pub.export_id=r.id
                     JOIN sync_captures c ON c.id=r.capture_id
                     WHERE r.state='succeeded' AND owner.state='succeeded'
                     AND owner.project_id=a.project_id AND owner.branch_id=a.branch_id
                     AND json_extract(owner.record,'$.capture_id')=c.id
                     AND pub.state='published'
                     AND json_extract(pub.descriptor,'$.manifest.capture_identity.generation')=c.id
                     AND json_extract(c.record,'$.project_id')=a.project_id
                     AND json_extract(c.record,'$.branch_id')=a.branch_id
                     AND r.rowid NOT IN (SELECT rowid FROM incremental_runs ORDER BY rowid DESC LIMIT ?1)
                     AND NOT EXISTS(SELECT 1 FROM incremental_heads h WHERE h.epoch_id=pub.epoch_id)
                     AND NOT EXISTS(SELECT 1 FROM snapshot_heads h WHERE h.epoch_id=pub.epoch_id)
                     AND NOT EXISTS(SELECT 1 FROM sync_runs live WHERE live.state IN ('queued','starting','running')
                         AND (json_extract(live.record,'$.apply_id')=r.id OR json_extract(live.record,'$.published_artifact_id')=r.id))
                     AND NOT EXISTS(SELECT 1 FROM native_processes n WHERE n.role='incremental-' || r.id)
                     AND NOT EXISTS(SELECT 1 FROM analytics_gc g WHERE g.export_id=r.id AND g.state='pending')
                     AND NOT EXISTS(SELECT 1 FROM incremental_requests q WHERE json_extract(q.response,'$.id')=r.id
                         AND NOT (q.project_id=a.project_id
                           AND json_extract(q.request,'$.owner') IS owner.id
                           AND json_extract(q.request,'$.command.kind') IS 'apply'
                           AND json_extract(q.request,'$.command.capture_id') IS r.capture_id
                           AND json_extract(q.request,'$.command.key') IS q.request_key))
                     ORDER BY r.rowid LIMIT ?2"
                )?.query_map(params![KEEP_RECENT, DELETE_BATCH], |r| r.get::<_,String>(0))?
                    .collect::<rusqlite::Result<Vec<_>>>()?;
                for id in ids {
                    // The engine must first stop/reassign the worker and remove its
                    // request workspace. Retention never performs process/file GC.
                    let work = self.root().join("analytics/apply-work").join(&id);
                    match std::fs::symlink_metadata(work) {
                        Ok(_) => continue,
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                        Err(e) => return Err(e.into()),
                    }
                    self.db.execute(
                        "DELETE FROM incremental_requests WHERE json_extract(response,'$.id')=?1",
                        [&id],
                    )?;
                    self.db
                        .execute("DELETE FROM incremental_runs WHERE id=?1", [&id])?;
                }
            }
            // Continuous parents have no user-issued admission receipt. Only
            // retire a success after every child has expired, retaining recent
            // parents and any explicit public request that names the run.
            let parents: i64 = self
                .db
                .query_row("SELECT count(*) FROM sync_runs", [], |r| r.get(0))?;
            if parents >= HIGH_WATER {
                self.db.execute(
                    "DELETE FROM sync_runs WHERE id IN (
                     SELECT r.id FROM sync_runs r WHERE r.state='succeeded'
                     AND json_extract(r.record,'$.trigger')='continuous'
                     AND json_extract(r.record,'$.config.mode')='continuous'
                     AND r.refresh_id IS NULL AND json_extract(r.record,'$.apply_id') IS NULL
                     AND r.ordinal NOT IN (SELECT ordinal FROM sync_runs ORDER BY ordinal DESC LIMIT ?1)
                     AND NOT EXISTS(SELECT 1 FROM incremental_runs a WHERE json_extract(a.record,'$.sync_run_id')=r.id)
                     AND NOT EXISTS(SELECT 1 FROM incremental_requests q WHERE json_extract(q.request,'$.owner')=r.id)
                     AND NOT EXISTS(SELECT 1 FROM sync_requests q WHERE
                         json_extract(q.response,'$.id')=r.id OR json_extract(q.request,'$.id')=r.id)
                     ORDER BY r.ordinal LIMIT ?2)", params![KEEP_RECENT,DELETE_BATCH])?;
            }
            Ok(())
        })();
        match result {
            Ok(()) => {
                self.db.execute_batch("RELEASE incremental_history")?;
                Ok(())
            }
            Err(e) => {
                self.db.execute_batch(
                    "ROLLBACK TO incremental_history; RELEASE incremental_history",
                )?;
                Err(e)
            }
        }
    }
}
