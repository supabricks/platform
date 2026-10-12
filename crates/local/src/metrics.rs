//! Metrics of each running branch: a sample a minute, kept for a week on
//! this machine and nowhere else. PostgreSQL is asked on a thread of its own,
//! so the daemon never waits for it; the daemon only writes what comes back.
//! Samples are telemetry, not state: they live in a database of their own,
//! are not part of a backup, and losing them loses nothing else.
use crate::{
    engine::Cell,
    store::{Result, Store, error::invalid},
};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    os::unix::fs::DirBuilderExt,
    path::Path,
    sync::mpsc::{Receiver, Sender, channel},
    time::{Duration, Instant},
};
use supabricks_core::resource::{BranchId, DesiredState};

/// How often a running branch is sampled.
pub const INTERVAL: Duration = Duration::from_secs(60);
/// How long a sample is kept.
pub const RETENTION: Duration = Duration::from_secs(7 * 24 * 60 * 60);

/// What PostgreSQL reported at one moment. The counters only ever grow while
/// a compute runs; a rate is the growth between two samples.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sample {
    pub at_ms: i64,
    pub active: i64,
    pub idle: i64,
    pub commits: i64,
    pub rollbacks: i64,
    pub blocks_hit: i64,
    pub blocks_read: i64,
    pub wal_bytes: i64,
    pub storage_bytes: i64,
}

const ASK: &str = "SELECT \
    (SELECT count(*) FROM pg_stat_activity a JOIN pg_roles r ON r.oid = a.usesysid \
       WHERE a.backend_type = 'client backend' AND NOT r.rolsuper AND a.state = 'active'), \
    (SELECT count(*) FROM pg_stat_activity a JOIN pg_roles r ON r.oid = a.usesysid \
       WHERE a.backend_type = 'client backend' AND NOT r.rolsuper AND a.state IS DISTINCT FROM 'active'), \
    d.xact_commit, d.xact_rollback, d.blks_hit, d.blks_read, \
    pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint, pg_database_size(d.datid) \
    FROM pg_stat_database d WHERE d.datname = 'postgres'";

struct Target {
    branch: BranchId,
    port: u16,
    password: String,
}
async fn ask(target: &Target) -> Option<Sample> {
    use tokio_postgres::{Config, NoTls};
    let work = async {
        let (client, connection) = Config::new()
            .host("127.0.0.1")
            .port(target.port)
            .user("cloud_admin")
            .password(&target.password)
            .dbname("postgres")
            .application_name("supabricks-metrics")
            .options("-c statement_timeout=2000 -c search_path=pg_catalog -c log_statement=none -c log_min_duration_statement=-1")
            .connect(NoTls)
            .await
            .ok()?;
        let connection = tokio::spawn(connection);
        let row = client.query_one(ASK, &[]).await.ok();
        connection.abort();
        let row = row?;
        Some(Sample {
            at_ms: chrono::Utc::now().timestamp_millis(),
            active: row.get(0),
            idle: row.get(1),
            commits: row.get(2),
            rollbacks: row.get(3),
            blocks_hit: row.get(4),
            blocks_read: row.get(5),
            wal_bytes: row.get(6),
            storage_bytes: row.get(7),
        })
    };
    tokio::time::timeout(Duration::from_secs(4), work)
        .await
        .ok()
        .flatten()
}
/// The thread that asks. A branch that does not answer yields no sample.
fn sampler(targets: Receiver<Vec<Target>>, samples: Sender<(BranchId, Sample)>) {
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        return;
    };
    while let Ok(round) = targets.recv() {
        for target in round {
            if let Some(sample) = runtime.block_on(ask(&target)) {
                if samples.send((target.branch, sample)).is_err() {
                    return;
                }
            }
        }
    }
}

/// The samples on disk.
pub struct Series(Connection);
impl Series {
    pub fn open(root: &Path) -> Result<Self> {
        let directory = root.join("metrics");
        if !directory.try_exists()? {
            std::fs::DirBuilder::new().mode(0o700).create(&directory)?;
        }
        let db = Connection::open(directory.join("samples.sqlite3"))?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        // A lost minute of telemetry is not worth a sync on every write.
        db.pragma_update(None, "synchronous", "NORMAL")?;
        db.execute_batch(
            "CREATE TABLE IF NOT EXISTS samples (
                branch_id TEXT NOT NULL, at_ms INTEGER NOT NULL,
                active INTEGER NOT NULL, idle INTEGER NOT NULL,
                commits INTEGER NOT NULL, rollbacks INTEGER NOT NULL,
                blocks_hit INTEGER NOT NULL, blocks_read INTEGER NOT NULL,
                wal_bytes INTEGER NOT NULL, storage_bytes INTEGER NOT NULL,
                PRIMARY KEY (branch_id, at_ms)) WITHOUT ROWID",
        )?;
        Ok(Self(db))
    }
    pub fn record(&self, branch: BranchId, s: &Sample) -> Result<()> {
        self.0.execute(
            "INSERT OR REPLACE INTO samples VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            params![
                branch.to_string(),
                s.at_ms,
                s.active,
                s.idle,
                s.commits,
                s.rollbacks,
                s.blocks_hit,
                s.blocks_read,
                s.wal_bytes,
                s.storage_bytes
            ],
        )?;
        Ok(())
    }
    pub fn prune(&self, before_ms: i64) -> Result<()> {
        self.0
            .execute("DELETE FROM samples WHERE at_ms < ?1", [before_ms])?;
        Ok(())
    }
    /// A branch's samples from `from_ms`, oldest first.
    pub fn since(&self, branch: BranchId, from_ms: i64) -> Result<Vec<Sample>> {
        let mut query = self.0.prepare(
            "SELECT at_ms,active,idle,commits,rollbacks,blocks_hit,blocks_read,wal_bytes,storage_bytes \
             FROM samples WHERE branch_id=?1 AND at_ms>=?2 ORDER BY at_ms",
        )?;
        let rows = query.query_map(params![branch.to_string(), from_ms], |r| {
            Ok(Sample {
                at_ms: r.get(0)?,
                active: r.get(1)?,
                idle: r.get(2)?,
                commits: r.get(3)?,
                rollbacks: r.get(4)?,
                blocks_hit: r.get(5)?,
                blocks_read: r.get(6)?,
                wal_bytes: r.get(7)?,
                storage_bytes: r.get(8)?,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }
}

/// The daemon's part: say whom to ask, and keep what was answered.
pub struct Metrics {
    pub series: Series,
    interval: Duration,
    targets: Sender<Vec<Target>>,
    samples: Receiver<(BranchId, Sample)>,
    asked: Option<Instant>,
    pruned: Option<Instant>,
}
impl Metrics {
    pub fn start(root: &Path) -> Result<Self> {
        let (targets, wanted) = channel();
        let (found, samples) = channel();
        std::thread::Builder::new()
            .name("metrics".into())
            .spawn(move || sampler(wanted, found))?;
        // Tests cannot wait a minute for a second sample.
        let interval = std::env::var("SUPABRICKS_TEST_METRICS_INTERVAL_MS")
            .ok()
            .and_then(|ms| ms.parse().ok())
            .map(Duration::from_millis)
            .unwrap_or(INTERVAL);
        Ok(Self {
            series: Series::open(root)?,
            interval,
            targets,
            samples,
            asked: None,
            pruned: None,
        })
    }
    pub fn interval(&self) -> Duration {
        self.interval
    }
    /// Called on every pass of the daemon. Never waits on PostgreSQL.
    pub fn tick(&mut self, store: &Store, cell: Option<&Cell>) -> Result<()> {
        while let Ok((branch, sample)) = self.samples.try_recv() {
            self.series.record(branch, &sample)?;
        }
        if self
            .pruned
            .is_none_or(|at| at.elapsed() > Duration::from_secs(3600))
        {
            self.series
                .prune(chrono::Utc::now().timestamp_millis() - RETENTION.as_millis() as i64)?;
            self.pruned = Some(Instant::now());
        }
        let Some(cell) = cell else {
            return Ok(());
        };
        if self.asked.is_some_and(|at| at.elapsed() < self.interval) {
            return Ok(());
        }
        self.asked = Some(Instant::now());
        let mut round = Vec::new();
        for branch in store.branches()? {
            // Only a compute that is up is asked: a suspended branch has a
            // gap, and is never woken to be measured.
            if branch.endpoint.desired_state != DesiredState::Running
                || store.is_export(branch.branch.id)?
                || !cell.connection_ready(store, &branch)?
            {
                continue;
            }
            if let Some(ports) = branch.ports {
                round.push(Target {
                    branch: branch.branch.id,
                    port: ports.sql,
                    password: store.endpoint_password(branch.endpoint.id)?,
                });
            }
        }
        self.targets
            .send(round)
            .map_err(|_| invalid("the metrics sampler has stopped"))
    }
}

/// Samples as evenly spaced points from `from_ms` to `to_ms`. A value is the
/// average of what was sampled in its step, or null where nothing was: a
/// suspended branch shows a gap, not a zero. A rate is the growth of a counter
/// between two samples taken close together; across a gap, or a restart of
/// the compute that set the counter back, there is no rate.
pub fn points(
    samples: &[Sample],
    from_ms: i64,
    to_ms: i64,
    step_ms: i64,
    interval_ms: i64,
) -> Vec<Value> {
    #[derive(Default)]
    struct Bucket {
        n: f64,
        active: f64,
        idle: f64,
        storage: f64,
        seconds: f64,
        commits: f64,
        rollbacks: f64,
        hit: f64,
        read: f64,
        wal: f64,
    }
    let count = ((to_ms - from_ms) / step_ms).max(0) as usize;
    let mut buckets: Vec<Bucket> = (0..count).map(|_| Bucket::default()).collect();
    let index = |at: i64| -> Option<usize> {
        let i = (at - from_ms).div_euclid(step_ms);
        (at >= from_ms && (i as usize) < count).then_some(i as usize)
    };
    for (i, s) in samples.iter().enumerate() {
        let Some(b) = index(s.at_ms) else {
            continue;
        };
        let bucket = &mut buckets[b];
        bucket.n += 1.0;
        bucket.active += s.active as f64;
        bucket.idle += s.idle as f64;
        bucket.storage += s.storage_bytes as f64;
        let Some(before) = i.checked_sub(1).map(|i| &samples[i]) else {
            continue;
        };
        let elapsed = s.at_ms - before.at_ms;
        let grew = [
            s.commits - before.commits,
            s.rollbacks - before.rollbacks,
            s.blocks_hit - before.blocks_hit,
            s.blocks_read - before.blocks_read,
            s.wal_bytes - before.wal_bytes,
        ];
        if elapsed <= 0 || elapsed > interval_ms * 5 / 2 || grew.iter().any(|d| *d < 0) {
            continue;
        }
        bucket.seconds += elapsed as f64 / 1000.0;
        bucket.commits += grew[0] as f64;
        bucket.rollbacks += grew[1] as f64;
        bucket.hit += grew[2] as f64;
        bucket.read += grew[3] as f64;
        bucket.wal += grew[4] as f64;
    }
    let round = |value: f64| (value * 1000.0).round() / 1000.0;
    buckets
        .iter()
        .enumerate()
        .map(|(i, b)| {
            let gauge = |sum: f64| (b.n > 0.0).then(|| round(sum / b.n));
            let rate = |sum: f64| (b.seconds > 0.0).then(|| round(sum / b.seconds));
            json!({
                "at": chrono::DateTime::from_timestamp_millis(from_ms + i as i64 * step_ms)
                    .unwrap_or_default()
                    .to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
                "connections_active": gauge(b.active),
                "connections_idle": gauge(b.idle),
                "commits_per_second": rate(b.commits),
                "rollbacks_per_second": rate(b.rollbacks),
                "cache_hit_ratio": (b.hit + b.read > 0.0).then(|| round(b.hit / (b.hit + b.read))),
                "wal_bytes_per_second": rate(b.wal),
                "storage_bytes": (b.n > 0.0).then(|| (b.storage / b.n).round() as i64),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(at_s: i64, commits: i64) -> Sample {
        Sample {
            at_ms: at_s * 1000,
            active: 2,
            idle: 1,
            commits,
            rollbacks: commits / 10,
            blocks_hit: commits * 9,
            blocks_read: commits,
            wal_bytes: commits * 100,
            storage_bytes: 8_000_000,
        }
    }
    #[test]
    fn a_rate_is_growth_between_neighbouring_samples() {
        let samples = [sample(0, 0), sample(60, 600), sample(120, 1800)];
        let found = points(&samples, 0, 180_000, 60_000, 60_000);
        assert_eq!(found.len(), 3);
        // Nothing came before the first sample, so it has gauges and no rate.
        assert_eq!(found[0]["connections_active"], 2.0);
        assert_eq!(found[0]["commits_per_second"], Value::Null);
        assert_eq!(found[1]["commits_per_second"], 10.0);
        assert_eq!(found[2]["commits_per_second"], 20.0);
        assert_eq!(found[2]["rollbacks_per_second"], 2.0);
        assert_eq!(found[2]["wal_bytes_per_second"], 2000.0);
        assert_eq!(found[2]["cache_hit_ratio"], 0.9);
        assert_eq!(found[2]["storage_bytes"], 8_000_000);
        assert_eq!(found[1]["at"], "1970-01-01T00:01:00.000Z");
    }
    #[test]
    fn a_gap_is_null_and_no_rate_spans_it_or_a_restart() {
        // Sampled, suspended for ten minutes, then running again with its
        // counters started over.
        let samples = [
            sample(0, 500),
            sample(60, 1100),
            sample(660, 30),
            sample(720, 90),
        ];
        let found = points(&samples, 0, 780_000, 60_000, 60_000);
        assert_eq!(found[1]["commits_per_second"], 10.0);
        for missing in &found[2..11] {
            assert!(
                missing
                    .as_object()
                    .unwrap()
                    .iter()
                    .all(|(k, v)| k == "at" || v.is_null())
            );
        }
        assert_eq!(found[11]["connections_active"], 2.0);
        assert_eq!(found[11]["commits_per_second"], Value::Null);
        assert_eq!(found[12]["commits_per_second"], 1.0);
        // The same gap with counters that kept growing still has no rate.
        let kept = [sample(0, 500), sample(660, 5000)];
        assert_eq!(
            points(&kept, 0, 720_000, 60_000, 60_000)[11]["commits_per_second"],
            Value::Null
        );
    }
    #[test]
    fn a_wide_step_averages_what_falls_in_it() {
        let samples: Vec<_> = (0..10).map(|i| sample(i * 60, i * 600)).collect();
        let found = points(&samples, 0, 600_000, 300_000, 60_000);
        assert_eq!(found.len(), 2);
        assert_eq!(found[0]["commits_per_second"], 10.0);
        assert_eq!(found[1]["commits_per_second"], 10.0);
        assert_eq!(found[0]["connections_idle"], 1.0);
        // Samples outside the window are not counted.
        assert!(points(&samples, 600_000, 900_000, 300_000, 60_000)[0]["storage_bytes"].is_null());
    }
    #[test]
    fn samples_are_kept_by_branch_and_pruned_by_age() {
        let root = tempfile::tempdir().unwrap();
        let series = Series::open(root.path()).unwrap();
        let (one, two) = (BranchId::new(), BranchId::new());
        for at in [10, 70, 130] {
            series.record(one, &sample(at, at)).unwrap();
        }
        series.record(two, &sample(70, 1)).unwrap();
        assert_eq!(series.since(one, 0).unwrap().len(), 3);
        assert_eq!(series.since(one, 70_000).unwrap()[0], sample(70, 70));
        assert_eq!(series.since(two, 0).unwrap().len(), 1);
        series.prune(100_000).unwrap();
        assert_eq!(series.since(one, 0).unwrap(), vec![sample(130, 130)]);
        assert!(series.since(two, 0).unwrap().is_empty());
        // Opening again finds what was written.
        drop(series);
        assert_eq!(
            Series::open(root.path())
                .unwrap()
                .since(one, 0)
                .unwrap()
                .len(),
            1
        );
    }
}
