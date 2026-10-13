use super::*;
use rusqlite::{Connection, OpenFlags, params};
use std::path::Path;
const MAX_ROWS: usize = 131_072;
pub(super) struct History {
    db: Connection,
    next_prune: Instant,
}
impl History {
    pub fn open(root: &Path) -> Result<Self> {
        // Resolve directory aliases (macOS /var -> /private/var), never the
        // database entry itself: its no-follow/private-file checks remain intact.
        let path = root.canonicalize()?.join("metrics.sqlite3");
        crate::store::ownership::private_file(&path)?.sync_all()?;
        let db = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        db.busy_timeout(Duration::from_millis(50))?;
        // A separate, disposable observation store: no control-journal migration.
        // DELETE journal avoids an unbounded WAL if an external reader pins it.
        db.execute_batch("PRAGMA page_size=4096; PRAGMA max_page_count=65536; PRAGMA journal_mode=DELETE; PRAGMA synchronous=NORMAL; PRAGMA cache_size=-2048;
            CREATE TABLE IF NOT EXISTS samples(project TEXT NOT NULL,branch TEXT NOT NULL,at_ms INTEGER NOT NULL,slot INTEGER NOT NULL,sample TEXT NOT NULL CHECK(length(sample)<=2048),PRIMARY KEY(project,branch,slot));
            CREATE INDEX IF NOT EXISTS samples_age ON samples(at_ms);
            CREATE INDEX IF NOT EXISTS samples_branch_age ON samples(project,branch,at_ms);")?;
        let mut history = Self {
            db,
            next_prune: Instant::now(),
        };
        history.prune(crate::identity::now())?;
        Ok(history)
    }
    pub fn prune(&mut self, now: i64) -> Result<()> {
        if Instant::now() < self.next_prune {
            return Ok(());
        }
        self.db.execute(
            "DELETE FROM samples WHERE at_ms < ?1 OR at_ms > ?2",
            params![now - RETENTION_MS, now],
        )?;
        self.next_prune = Instant::now() + Duration::from_secs(60);
        Ok(())
    }
    pub fn append(&mut self, project: ProjectId, branch: BranchId, sample: &Sample) -> Result<()> {
        self.prune(sample.at_ms)?;
        let record = serde_json::to_string(sample)?;
        if record.len() > 2048 {
            return Err(conflict("metrics sample exceeds bound"));
        }
        let tx = self.db.transaction()?;
        let count: i64 = tx.query_row("SELECT count(*) FROM samples", [], |r| r.get(0))?;
        if count >= MAX_ROWS as i64 {
            // Batched eviction amortizes retention work and bounds all projects together.
            tx.execute("DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples ORDER BY at_ms LIMIT ?1)",[(count-MAX_ROWS as i64+256).max(256)])?;
        }
        tx.execute(
            "INSERT OR REPLACE INTO samples VALUES (?1,?2,?3,?4,?5)",
            params![
                project.to_string(),
                branch.to_string(),
                sample.at_ms,
                sample.at_ms / INTERVAL_MS,
                record
            ],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn read(
        &self,
        project: ProjectId,
        branch: BranchId,
        range: Range,
        now: i64,
    ) -> Result<Value> {
        let (duration, step) = range.bounds();
        let end = now / step * step;
        let start = end - duration;
        let mut statement=self.db.prepare("SELECT sample FROM samples WHERE project=?1 AND branch=?2 AND at_ms>=?3 AND at_ms<=?4 ORDER BY at_ms LIMIT 20165")?;
        let samples = statement
            .query_map(
                params![
                    project.to_string(),
                    branch.to_string(),
                    (start - INTERVAL_MS * 3).max(now - RETENTION_MS),
                    now
                ],
                |r| r.get::<_, String>(0),
            )?
            .map(|r| Ok(serde_json::from_str::<Sample>(&r?)?))
            .collect::<Result<Vec<_>>>()?;
        let oldest:Option<i64>=self.db.query_row("SELECT min(at_ms) FROM samples WHERE project=?1 AND branch=?2 AND at_ms>=?3 AND at_ms<=?4",params![project.to_string(),branch.to_string(),now-RETENTION_MS,now],|r|r.get(0))?;
        let mut points = Vec::new();
        let mut index = 0;
        let mut previous = None;
        for bucket in (start..=end).step_by(step as usize) {
            let mut point = json!({"observed_at_ms":null,"status":"missing","connections_active":null,"connections_idle":null,"connections_other":null,"database_size_bytes":null,"commits_per_second":null,"rollbacks_per_second":null,"wal_bytes_per_second":null,"cache_hit_ratio":null,"rate_interval_ms":null,"rates_status":"sample_unavailable"});
            let mut count = 0;
            let mut available = 0;
            while index < samples.len() && samples[index].at_ms < bucket + step {
                let sample = &samples[index];
                if sample.at_ms >= bucket {
                    point = sample.point(previous);
                    count += 1;
                    if sample.raw.is_some() {
                        available += 1;
                    }
                }
                previous = Some(sample);
                index += 1;
            }
            point["bucket_start_ms"] = json!(bucket);
            point["samples"] = json!(count);
            point["available_samples"] = json!(available);
            points.push(point);
        }
        Ok(
            json!({"format_version":1,"range":range,"from_ms":start,"to_ms":now,"step_ms":step,"aggregation":"last_observation_in_bucket","sample_interval_ms":INTERVAL_MS,"retention_ms":RETENTION_MS,"max_samples_per_installation":MAX_ROWS,"max_database_bytes":256*1024*1024,"max_branches":MAX_BRANCHES,"oldest_retained_at_ms":oldest,"points":points,"scope":{"database":"postgres","connections":"client backends, excluding this sampler; idle includes idle in transaction","transactions":"database-wide, includes platform monitoring traffic","wal":"compute-wide generated bytes, not retained WAL size","storage":"pg_database_size; not installation disk usage or object-store footprint","cache":"interval PostgreSQL buffer hits / (hits + reads), not OS cache"},"unavailable":{"cpu":"requires_compute_process_accounting","memory":"requires_compute_process_accounting","query_latency_percentiles":"histogram_not_collected"},"query_text_persisted":false,"external_telemetry":false,"governed_supported":false}),
        )
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn directory_aliases_work_but_database_symlinks_are_refused() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("real");
        std::fs::create_dir(&root).unwrap();
        let alias = dir.path().join("alias");
        symlink(&root, &alias).unwrap();
        drop(History::open(&alias).unwrap());
        let database = root.join("metrics.sqlite3");
        let original = root.join("original.sqlite3");
        std::fs::rename(&database, &original).unwrap();
        let before = std::fs::read(&original).unwrap();
        symlink(&original, &database).unwrap();
        assert!(History::open(&alias).is_err());
        assert_eq!(std::fs::read(original).unwrap(), before);
    }
    #[test]
    fn installation_cap_retention_and_same_slot_replacement() {
        let dir = tempfile::tempdir().unwrap();
        let mut history = History::open(dir.path()).unwrap();
        let now = crate::identity::now();
        // Seed a full installation without issuing 131k individual transactions.
        history.db.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?1) INSERT INTO samples SELECT 'old','b',?2,i,'{}' FROM n",params![MAX_ROWS as i64,now-1000]).unwrap();
        let project = ProjectId::new();
        let branch = BranchId::new();
        let mut sample = Sample {
            at_ms: now,
            generation: 1,
            revision: 1,
            status: "suspended".into(),
            raw: None,
        };
        history.append(project, branch, &sample).unwrap();
        assert_eq!(
            history
                .db
                .query_row("SELECT count(*) FROM samples", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            MAX_ROWS as i64 - 255
        );
        sample.generation = 2;
        history.append(project, branch, &sample).unwrap();
        assert_eq!(
            history
                .db
                .query_row(
                    "SELECT count(*) FROM samples WHERE project=?1",
                    [project.to_string()],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
        history.next_prune = Instant::now();
        history.prune(now + RETENTION_MS + 1).unwrap();
        assert_eq!(
            history
                .db
                .query_row("SELECT count(*) FROM samples", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
        let pages: i64 = history
            .db
            .pragma_query_value(None, "max_page_count", |r| r.get(0))
            .unwrap();
        let size: i64 = history
            .db
            .pragma_query_value(None, "page_size", |r| r.get(0))
            .unwrap();
        assert_eq!(pages * size, 256 * 1024 * 1024);
    }
}
