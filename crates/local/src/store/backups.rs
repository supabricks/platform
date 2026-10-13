//! Backups as the platform API reports them, and the schedule that takes them.
//! A backup is held by a branch record with no compute; see catalog 34.
use super::{Result, Store, error::missing, now_ms, parse};
use crate::operations::{Mutation, Operation};
use rusqlite::{OptionalExtension, params};
use supabricks_core::lsn::Lsn;
use supabricks_core::resource::{BranchId, DesiredState, ProjectId};

/// How long the schedule waits before trying again after it could not start.
const RETRY_MS: i64 = 5 * 60 * 1000;

/// The length of a schedule's hour. Tests cannot wait an hour for a second
/// scheduled backup.
fn hour_ms() -> i64 {
    static HOUR: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
    *HOUR.get_or_init(|| {
        std::env::var("SUPABRICKS_TEST_BACKUP_HOUR_MS")
            .ok()
            .and_then(|ms| ms.parse().ok())
            .filter(|ms| *ms > 0)
            .unwrap_or(3_600_000)
    })
}

#[derive(Clone, Debug, PartialEq)]
pub struct BackupView {
    /// The record that holds the backup.
    pub id: BranchId,
    pub project: ProjectId,
    pub database: BranchId,
    /// The branch it was taken from.
    pub source: BranchId,
    pub source_name: String,
    pub label: String,
    pub origin: String,
    pub state: &'static str,
    pub created_at_ms: i64,
    /// Where in the source's history it stands, once that is known.
    pub lsn: Option<Lsn>,
    /// Live branches restored from it.
    pub restored: i64,
    pub lifecycle_revision: i64,
    /// Why it failed, if it did.
    pub error: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct BackupPolicy {
    pub every_hours: i64,
    pub keep: i64,
    pub revision: i64,
    pub updated_at_ms: i64,
    /// When the next scheduled backup is due, if the last one is known.
    pub next_at_ms: i64,
}

impl Store {
    /// The backups of every branch of a database, newest first.
    pub fn backup_views(&self, project: ProjectId, database: BranchId) -> Result<Vec<BackupView>> {
        let rows: Vec<(String, String, String, String, i64)> = self
            .db
            .prepare(
                "SELECT k.branch_id,k.source_id,k.label,k.origin,k.created_at_ms FROM backups k JOIN branches b ON b.id=k.branch_id WHERE b.project_id=?1",
            )?
            .query_map([project.to_string()], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
            })?
            .collect::<rusqlite::Result<_>>()?;
        let mut views = Vec::new();
        for (hold, source, label, origin, created_at_ms) in rows {
            let id: BranchId = parse(&hold)?;
            let source: BranchId = parse(&source)?;
            let record = self.branch(id)?;
            let gone = record.endpoint.desired_state == DesiredState::Deleted;
            let converged = record.revision == record.observed_revision;
            // Its files are removed: it no longer exists.
            if gone && converged {
                continue;
            }
            if self.database_of(project, source)? != Some(database) {
                continue;
            }
            let error: Option<String> = self
                .db
                .query_row(
                    "SELECT json_extract(e.detail,'$.message') FROM operations o JOIN operation_errors e ON e.operation_id=o.id WHERE o.branch_id=?1 AND e.terminal=1 ORDER BY o.rowid LIMIT 1",
                    [id.to_string()],
                    |r| r.get(0),
                )
                .optional()?
                .flatten();
            let state = if gone {
                "deleting"
            } else if record.timeline_created && converged {
                "ready"
            } else if error.is_some() {
                "failed"
            } else {
                "creating"
            };
            let restored: i64 = self.db.query_row(
                "SELECT count(*) FROM branches WHERE parent_id=?1 AND (desired!='deleted' OR observed_revision!=revision)",
                [id.to_string()],
                |r| r.get(0),
            )?;
            views.push(BackupView {
                id,
                project,
                database,
                source,
                source_name: self.branch(source)?.branch.name,
                label,
                origin,
                state,
                created_at_ms,
                lsn: record.branch.ancestor_lsn,
                restored,
                lifecycle_revision: record.revision,
                error: error.filter(|_| state == "failed"),
            });
        }
        views.sort_by(|a, b| {
            (b.created_at_ms, b.id.to_string()).cmp(&(a.created_at_ms, a.id.to_string()))
        });
        Ok(views)
    }
    pub fn backup_view(
        &self,
        project: ProjectId,
        database: BranchId,
        id: BranchId,
    ) -> Result<BackupView> {
        self.backup_views(project, database)?
            .into_iter()
            .find(|view| view.id == id)
            .ok_or_else(|| missing("backup in database"))
    }
    pub fn backup_label(&self, id: BranchId) -> Result<Option<String>> {
        Ok(self
            .db
            .query_row(
                "SELECT label FROM backups WHERE branch_id=?1",
                [id.to_string()],
                |r| r.get(0),
            )
            .optional()?)
    }
    /// The schedule of a database, if it has one.
    pub fn backup_policy(&self, database: BranchId) -> Result<Option<BackupPolicy>> {
        let Some((every_hours, keep, revision, updated_at_ms, attempted)) = self
            .db
            .query_row(
                "SELECT every_hours,keep,revision,updated_at_ms,attempted_at_ms FROM backup_policies WHERE database_id=?1",
                [database.to_string()],
                |r| {
                    Ok((
                        r.get::<_, i64>(0)?,
                        r.get::<_, i64>(1)?,
                        r.get::<_, i64>(2)?,
                        r.get::<_, i64>(3)?,
                        r.get::<_, Option<i64>>(4)?,
                    ))
                },
            )
            .optional()?
        else {
            return Ok(None);
        };
        // The first backup is due as soon as the schedule exists.
        let last: Option<i64> = self.db.query_row(
            "SELECT max(k.created_at_ms) FROM backups k WHERE k.source_id=?1 AND k.origin='scheduled'",
            [database.to_string()],
            |r| r.get(0),
        )?;
        let due = last.map_or(updated_at_ms, |at| at + every_hours * hour_ms());
        Ok(Some(BackupPolicy {
            every_hours,
            keep,
            revision,
            updated_at_ms,
            next_at_ms: due.max(attempted.map_or(0, |at| at + RETRY_MS.min(hour_ms()))),
        }))
    }
    /// Set or remove the schedule of a database.
    pub fn set_backup_policy(
        &mut self,
        database: BranchId,
        policy: Option<(i64, i64)>,
    ) -> Result<()> {
        match policy {
            None => {
                self.db.execute(
                    "DELETE FROM backup_policies WHERE database_id=?1",
                    [database.to_string()],
                )?;
            }
            Some((every_hours, keep)) => {
                self.db.execute(
                    "INSERT INTO backup_policies(database_id,every_hours,keep,updated_at_ms) VALUES (?1,?2,?3,?4)
                     ON CONFLICT(database_id) DO UPDATE SET every_hours=excluded.every_hours,keep=excluded.keep,
                      revision=revision+1,updated_at_ms=excluded.updated_at_ms,attempted_at_ms=NULL",
                    params![database.to_string(), every_hours, keep, now_ms()?],
                )?;
            }
        }
        Ok(())
    }
    /// Take the scheduled backups that are due and remove the ones a schedule
    /// no longer keeps. Returns what it started.
    pub fn run_backup_schedules(&mut self) -> Result<Vec<Operation>> {
        let now = now_ms()?;
        let databases: Vec<String> = self
            .db
            .prepare("SELECT database_id FROM backup_policies")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        let mut started = Vec::new();
        for database in databases {
            let id: BranchId = parse(&database)?;
            let root = self.branch(id)?;
            let project = root.branch.project_id;
            if root.endpoint.desired_state == DesiredState::Deleted {
                // The schedule goes with its database.
                self.set_backup_policy(id, None)?;
                continue;
            }
            let Some(policy) = self.backup_policy(id)? else {
                continue;
            };
            let views: Vec<_> = self
                .backup_views(project, id)?
                .into_iter()
                .filter(|view| view.source == id && view.origin == "scheduled")
                .collect();
            // Newest first. A failed one holds nothing; one that a branch was
            // restored from cannot go until that branch does.
            let mut kept = 0;
            for view in &views {
                let surplus = match view.state {
                    "ready" => {
                        kept += 1;
                        kept > policy.keep
                    }
                    "failed" => true,
                    _ => false,
                };
                if surplus && view.restored == 0 {
                    let key = format!("internal:backup:prune:{}", view.id);
                    let remove = Mutation::SetState {
                        branch_id: view.id,
                        expected_revision: view.lifecycle_revision,
                        desired: DesiredState::Deleted,
                    };
                    // A refusal is tried again on a later pass.
                    if let Ok(operation) = self.submit(project, &key, remove) {
                        started.push(operation);
                    }
                }
            }
            // A suspended database is left asleep: it has not changed since it
            // was suspended, and a backup would have to wake it.
            let awake = root.endpoint.desired_state == DesiredState::Running
                && root.revision == root.observed_revision
                && !root.expired;
            let underway = views.iter().any(|view| view.state == "creating");
            if now < policy.next_at_ms || !awake || underway {
                continue;
            }
            self.db.execute(
                "UPDATE backup_policies SET attempted_at_ms=?2 WHERE database_id=?1",
                params![database, now],
            )?;
            let take = Mutation::Hold {
                parent_id: id,
                label: format!(
                    "Scheduled {}",
                    chrono::DateTime::from_timestamp_millis(now)
                        .unwrap_or_default()
                        .format("%Y-%m-%d %H:%M UTC")
                ),
                origin: "scheduled".into(),
                timeout_ms: crate::operations::branch_timeout(),
            };
            if let Ok(operation) =
                self.submit(project, &format!("internal:backup:{database}:{now}"), take)
            {
                started.push(operation);
            }
        }
        Ok(started)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::operations::{BranchPoint, Ports, Step};
    use crate::project::ProjectConfig;
    use serde_json::json;

    fn setup() -> (tempfile::TempDir, Store, ProjectId, BranchId) {
        let root = tempfile::tempdir().unwrap();
        let mut store = Store::open(&root.path().join("cell")).unwrap();
        let project = ProjectId::new();
        store
            .register_project(&ProjectConfig {
                format_version: 1,
                id: project,
                name: "project".into(),
            })
            .unwrap();
        let main = store
            .submit(
                project,
                "main",
                Mutation::CreateDatabase {
                    name: "main".into(),
                    ports: ports(5500),
                },
            )
            .unwrap();
        finish(&mut store, &main);
        (root, store, project, main.branch_id)
    }
    fn ports(n: u16) -> Ports {
        Ports {
            sql: n,
            external_http: n + 1,
            internal_http: n + 2,
        }
    }
    /// Carry an operation through as the engines would.
    fn finish(store: &mut Store, operation: &Operation) {
        while let Some(ticket) = store.ticket(operation.id).unwrap() {
            if ticket.step == Step::CaptureBranchPoint {
                store.pin_lsn(&ticket, "0/3000".parse().unwrap()).unwrap();
            }
            store
                .checkpoint(&ticket, json!({"effect": ticket.idempotency_key()}))
                .unwrap();
        }
        // The engines release a parent once nothing is being made from it.
        store.reconcile_parent_pins().unwrap();
    }
    fn hold(
        store: &mut Store,
        project: ProjectId,
        source: BranchId,
        key: &str,
        origin: &str,
    ) -> Operation {
        store
            .submit(
                project,
                key,
                Mutation::Hold {
                    parent_id: source,
                    label: key.into(),
                    origin: origin.into(),
                    timeout_ms: 90_000,
                },
            )
            .unwrap()
    }
    fn refused(result: Result<Operation>, why: &str) {
        let error = result.unwrap_err().to_string();
        assert!(error.contains(why), "{error}");
    }

    #[test]
    fn a_backup_is_held_without_a_compute_and_is_not_a_branch() {
        let (_root, mut store, project, main) = setup();
        let taken = hold(&mut store, project, main, "first", "manual");
        assert_eq!(
            taken.steps,
            [Step::CaptureBranchPoint, Step::EnsureTimeline]
        );
        let record = store.branch(taken.branch_id).unwrap();
        assert!(record.ports.is_none(), "nothing listens for it");
        assert_eq!(record.endpoint.desired_state, DesiredState::Suspended);
        let view = store.backup_view(project, main, taken.branch_id).unwrap();
        assert_eq!(
            (view.state, view.lsn, view.source),
            ("creating", None, main)
        );
        finish(&mut store, &taken);
        let view = store.backup_view(project, main, taken.branch_id).unwrap();
        assert_eq!(view.state, "ready");
        assert_eq!(view.lsn, Some("0/3000".parse().unwrap()));
        assert_eq!(
            (view.label.as_str(), view.origin.as_str()),
            ("first", "manual")
        );

        // To everything that reads branches it does not exist.
        let branches = store.branch_views(project, main).unwrap();
        assert_eq!(branches.len(), 1);
        assert_eq!((branches[0].children, branches[0].backups), (0, 1));
        let database = store.database_record(project, main).unwrap();
        assert_eq!((database.branches, database.backups), (1, 1));
        assert_eq!(store.database_of(project, taken.branch_id).unwrap(), None);
        assert!(store.accepting_work(taken.branch_id).is_err());
    }

    #[test]
    fn a_backup_can_only_be_restored_from_or_deleted() {
        let (_root, mut store, project, main) = setup();
        let taken = hold(&mut store, project, main, "kept", "manual");
        finish(&mut store, &taken);
        let id = taken.branch_id;
        let state = |desired| Mutation::SetState {
            branch_id: id,
            expected_revision: 1,
            desired,
        };
        refused(
            store.submit(project, "run", state(DesiredState::Running)),
            "can only be deleted",
        );
        refused(
            store.submit(
                project,
                "ttl",
                Mutation::SetTtl {
                    branch_id: id,
                    expected_revision: 1,
                    expires_at_ms: Some(i64::MAX),
                },
            ),
            "can only be deleted",
        );
        refused(
            store.submit(project, "default", Mutation::SetDefault { branch_id: id }),
            "can only be deleted",
        );
        refused(
            store.submit(
                project,
                "again",
                Mutation::Hold {
                    parent_id: id,
                    label: "again".into(),
                    origin: "manual".into(),
                    timeout_ms: 90_000,
                },
            ),
            "cannot back up a backup",
        );
        let from = |key: &str, point| Mutation::BranchFrom {
            name: key.into(),
            parent_id: id,
            ports: ports(5600),
            point,
            timeout_ms: 90_000,
        };
        let earlier = BranchPoint::Lsn {
            lsn: "0/1000".parse().unwrap(),
        };
        refused(
            store.submit(project, "earlier", from("earlier", earlier)),
            "starts at the backup",
        );

        // A branch restored from it stands under the branch it was taken from.
        let restored = store
            .submit(project, "restored", from("restored", BranchPoint::Head))
            .unwrap();
        let view = store
            .branch_view(project, main, restored.branch_id)
            .unwrap();
        assert_eq!((view.parent, view.backup), (Some(main), Some(id)));
        let source = store.branch_view(project, main, main).unwrap();
        assert_eq!((source.children, source.backups), (1, 1));
        assert_eq!(store.backup_view(project, main, id).unwrap().restored, 1);
        finish(&mut store, &restored);
        refused(
            store.submit(project, "gone", state(DesiredState::Deleted)),
            "delete child branches",
        );
        let remove = store
            .submit(
                project,
                "restored-gone",
                Mutation::SetState {
                    branch_id: restored.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Deleted,
                },
            )
            .unwrap();
        finish(&mut store, &remove);
        let gone = store
            .submit(project, "gone", state(DesiredState::Deleted))
            .unwrap();
        assert_eq!(
            store.backup_view(project, main, id).unwrap().state,
            "deleting"
        );
        finish(&mut store, &gone);
        assert!(store.backup_views(project, main).unwrap().is_empty());
        assert_eq!(store.branch_view(project, main, main).unwrap().backups, 0);
    }

    #[test]
    fn a_label_and_an_origin_are_checked() {
        let (_root, mut store, project, main) = setup();
        for (label, origin) in [("", "manual"), ("line\nbreak", "manual"), ("ok", "other")] {
            let request = Mutation::Hold {
                parent_id: main,
                label: label.into(),
                origin: origin.into(),
                timeout_ms: 90_000,
            };
            assert!(
                store.submit(project, "bad", request).is_err(),
                "{label:?} {origin}"
            );
        }
        let long = Mutation::Hold {
            parent_id: main,
            label: "x".repeat(65),
            origin: "manual".into(),
            timeout_ms: 90_000,
        };
        assert!(store.submit(project, "long", long).is_err());
        assert!(store.backup_views(project, main).unwrap().is_empty());
    }

    #[test]
    fn a_schedule_takes_what_is_due_and_keeps_the_newest() {
        let (_root, mut store, project, main) = setup();
        assert_eq!(store.backup_policy(main).unwrap(), None);
        assert!(store.run_backup_schedules().unwrap().is_empty());
        store.set_backup_policy(main, Some((24, 2))).unwrap();
        let policy = store.backup_policy(main).unwrap().unwrap();
        assert_eq!(
            (policy.every_hours, policy.keep, policy.revision),
            (24, 2, 1)
        );
        assert_eq!(
            policy.next_at_ms, policy.updated_at_ms,
            "the first is due at once"
        );

        let first = store.run_backup_schedules().unwrap();
        assert_eq!(first.len(), 1);
        assert!(
            store.run_backup_schedules().unwrap().is_empty(),
            "one at a time"
        );
        finish(&mut store, &first[0]);
        let view = store
            .backup_view(project, main, first[0].branch_id)
            .unwrap();
        assert_eq!((view.origin.as_str(), view.state), ("scheduled", "ready"));
        assert!(view.label.starts_with("Scheduled "));
        let next = store.backup_policy(main).unwrap().unwrap().next_at_ms;
        assert_eq!(next, view.created_at_ms + 24 * hour_ms());
        assert!(store.run_backup_schedules().unwrap().is_empty(), "not due");

        // Two more by the schedule and one by hand, each a moment apart.
        let manual = hold(&mut store, project, main, "by hand", "manual");
        finish(&mut store, &manual);
        for (n, key) in ["second", "third"].into_iter().enumerate() {
            let taken = hold(&mut store, project, main, key, "scheduled");
            finish(&mut store, &taken);
            store
                .db
                .execute(
                    "UPDATE backups SET created_at_ms=created_at_ms+?2 WHERE branch_id=?1",
                    params![taken.branch_id.to_string(), n as i64 + 1],
                )
                .unwrap();
        }
        let removed = store.run_backup_schedules().unwrap();
        assert_eq!(removed.len(), 1, "the oldest scheduled one goes");
        assert_eq!(removed[0].branch_id, first[0].branch_id);
        finish(&mut store, &removed[0]);
        let left: Vec<_> = store
            .backup_views(project, main)
            .unwrap()
            .into_iter()
            .map(|view| view.label)
            .collect();
        assert_eq!(left, ["third", "second", "by hand"]);
        assert!(store.run_backup_schedules().unwrap().is_empty());

        // A suspended database is left asleep however due its schedule is.
        store.set_backup_policy(main, Some((1, 2))).unwrap();
        store
            .db
            .execute(
                "UPDATE backups SET created_at_ms=0 WHERE origin='scheduled'",
                [],
            )
            .unwrap();
        let asleep = store
            .submit(
                project,
                "asleep",
                Mutation::SetState {
                    branch_id: main,
                    expected_revision: 1,
                    desired: DesiredState::Suspended,
                },
            )
            .unwrap();
        assert!(store.run_backup_schedules().unwrap().is_empty());
        while let Some(ticket) = store.ticket(asleep.id).unwrap() {
            if ticket.step == Step::CaptureSuspend {
                store
                    .capture_suspend_lsn(&ticket, "0/4000".parse().unwrap())
                    .unwrap();
            }
            store.checkpoint(&ticket, json!({})).unwrap();
        }
        assert!(store.run_backup_schedules().unwrap().is_empty());
        store.set_backup_policy(main, None).unwrap();
        assert_eq!(store.backup_policy(main).unwrap(), None);
        assert_eq!(store.backup_views(project, main).unwrap().len(), 3);
    }
}
