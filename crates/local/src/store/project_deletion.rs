//! Deleting a project. The request hides the project and frees its name at
//! once; the daemon then removes its branches and files step by step. Rows
//! stay as a tombstone, as deleted branches do.
//!
//! This version deletes PostgreSQL data and project files. It refuses while
//! the project still has sync pipelines, publications, analytical data or
//! running work, because nothing yet reclaims those as part of a delete.
use super::{
    Result, Store,
    error::{conflict, missing},
    project_directory::revision_mismatch,
};
use crate::operations::Mutation;
use rusqlite::{OptionalExtension, params};
use supabricks_core::resource::{DesiredState, ProjectId};

/// One reason a project cannot be deleted yet, and how many things cause it.
#[derive(Clone, Debug, PartialEq)]
pub struct Blocker {
    pub reason: &'static str,
    pub count: i64,
}
#[derive(Clone, Debug, PartialEq)]
pub struct DeletionImpact {
    pub databases: i64,
    pub branches: i64,
    pub blockers: Vec<Blocker>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Deletion {
    pub deployment_id: String,
    pub operation_id: String,
    pub state: String,
    pub former_name: String,
    pub waiting_on: Option<String>,
}

/// Each reason with a query that counts it. The query takes one parameter:
/// the deployment ID, or the runtime project ID where the flag is set.
const BLOCKERS: [(&str, bool, &str); 5] = [
    (
        "read_by_another_project",
        false,
        "SELECT count(DISTINCT r.reference_key) FROM catalog_publication_refs r JOIN catalog_publications p ON p.id=r.publication_id WHERE p.deployment_id=?1 AND r.reference_key NOT LIKE 'binding:'||?1||':%'",
    ),
    (
        "sync_pipelines",
        true,
        "SELECT count(*) FROM sync_policies WHERE project_id=?1 AND state!='deleted'",
    ),
    (
        "publications",
        false,
        "SELECT count(*) FROM catalog_publications WHERE deployment_id=?1 AND state!='retired'",
    ),
    (
        "analytical_data",
        true,
        "SELECT count(*) FROM analytical_artifacts a WHERE a.project_id=?1 AND NOT EXISTS (SELECT 1 FROM analytics_gc g WHERE g.export_id=a.id AND g.state='done')",
    ),
    (
        "running_work",
        true,
        "SELECT (SELECT count(*) FROM ingest_jobs WHERE project_id=?1 AND state IN ('queued','loading','reconciling')) + (SELECT count(*) FROM analytical_sessions WHERE project_id=?1 AND state IN ('waiting','starting','ready','closing'))",
    ),
];

impl Store {
    fn runtime_project(&self, deployment: &str) -> Result<String> {
        self.db
            .query_row(
                "SELECT runtime_project_id FROM deployments WHERE id=?1",
                [deployment],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("project"))
    }
    /// What deleting the project would remove, and what stops it.
    pub fn project_deletion_impact(&self, deployment: &str) -> Result<DeletionImpact> {
        let record = self.project_record(deployment)?;
        let runtime = self.runtime_project(deployment)?;
        let mut blockers = Vec::new();
        for (reason, by_runtime, sql) in BLOCKERS {
            let id = if by_runtime {
                runtime.as_str()
            } else {
                deployment
            };
            let count: i64 = self.db.query_row(sql, [id], |r| r.get(0))?;
            if count > 0 {
                blockers.push(Blocker { reason, count });
            }
        }
        Ok(DeletionImpact {
            databases: record.databases,
            branches: record.branches,
            blockers,
        })
    }
    pub fn project_deletion(&self, deployment: &str) -> Result<Option<Deletion>> {
        Ok(self
            .db
            .query_row(
                "SELECT deployment_id,operation_id,state,former_name,waiting_on FROM project_deletions WHERE deployment_id=?1",
                [deployment],
                |r| {
                    Ok(Deletion {
                        deployment_id: r.get(0)?,
                        operation_id: r.get(1)?,
                        state: r.get(2)?,
                        former_name: r.get(3)?,
                        waiting_on: r.get(4)?,
                    })
                },
            )
            .optional()?)
    }
    /// Start deleting a project. Repeating the call with the same
    /// `operation_id` is harmless. Refused if the project changed since
    /// `expected_revision`, or while something blocks deletion.
    pub fn delete_project(
        &mut self,
        deployment: &str,
        expected_revision: i64,
        operation_id: &str,
    ) -> Result<()> {
        if let Some(existing) = self.project_deletion(deployment)? {
            return if existing.operation_id == operation_id {
                Ok(())
            } else {
                Err(missing("project"))
            };
        }
        let record = self.project_record(deployment)?;
        if record.revision != expected_revision {
            return Err(revision_mismatch());
        }
        let impact = self.project_deletion_impact(deployment)?;
        if !impact.blockers.is_empty() {
            let reasons: Vec<_> = impact
                .blockers
                .iter()
                .map(|b| format!("{} ({})", b.reason, b.count))
                .collect();
            return Err(conflict(&format!(
                "delete_blocked: remove these first: {}",
                reasons.join(", ")
            )));
        }
        let runtime = self.runtime_project(deployment)?;
        let root = self.root().to_owned();
        let tx = self.db.transaction()?;
        // Only folders the server made are its to remove; a checkout elsewhere
        // on the machine belongs to the person.
        let mut owned: Vec<String> = tx
            .prepare("SELECT path FROM worktree_bindings WHERE deployment_id=?1")?
            .query_map([deployment], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?
            .into_iter()
            .filter(|path| {
                ["console-created-projects", "console-projects"]
                    .iter()
                    .any(|folder| std::path::Path::new(path).starts_with(root.join(folder)))
            })
            .collect();
        owned.push(
            root.join("project-revisions")
                .join(deployment)
                .to_string_lossy()
                .into_owned(),
        );
        tx.execute(
            "INSERT INTO project_deletions VALUES (?1,?2,'running',?3,?4,NULL)",
            params![
                deployment,
                operation_id,
                record.name,
                serde_json::to_string(&owned)?
            ],
        )?;
        tx.execute(
            "INSERT INTO project_details(deployment_id,created_at_ms,deleted_at_ms) VALUES (?1,?2,?2)
             ON CONFLICT(deployment_id) DO UPDATE SET deleted_at_ms=excluded.deleted_at_ms",
            params![deployment, chrono::Utc::now().timestamp_millis()],
        )?;
        // Frees the name. The placeholder cannot collide: it is not a legal name.
        tx.execute(
            "UPDATE projects SET name=?1 WHERE id=?2",
            params![format!("deleted:{deployment}"), runtime],
        )?;
        // Nothing can be run against the project from a checkout any more.
        tx.execute(
            "DELETE FROM worktree_bindings WHERE deployment_id=?1",
            [deployment],
        )?;
        tx.execute(
            "UPDATE deployments SET revision=revision+1 WHERE id=?1",
            [deployment],
        )?;
        tx.commit()?;
        Ok(())
    }
    /// Whether the project is deleted or being deleted.
    pub(crate) fn project_deleted(&self, deployment: &str) -> Result<bool> {
        Ok(self
            .db
            .prepare("SELECT 1 FROM project_details WHERE deployment_id=?1 AND deleted_at_ms IS NOT NULL")?
            .exists([deployment])?)
    }
    /// Move every unfinished deletion forward as far as it can go now. Safe to
    /// call at any time and after a restart: each step looks at what is left.
    pub(crate) fn advance_project_deletions(&mut self) -> Result<()> {
        let running: Vec<String> = self
            .db
            .prepare("SELECT deployment_id FROM project_deletions WHERE state='running'")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        for deployment in running {
            let waiting = match self.advance_project_deletion(&deployment) {
                Ok(waiting) => waiting,
                Err(error) => Some(error.to_string()),
            };
            self.db.execute(
                "UPDATE project_deletions SET waiting_on=?2 WHERE deployment_id=?1 AND state='running'",
                params![deployment, waiting],
            )?;
        }
        Ok(())
    }
    /// One pass. Returns what it is waiting on, or `None` once finished.
    fn advance_project_deletion(&mut self, deployment: &str) -> Result<Option<String>> {
        let deletion = self
            .project_deletion(deployment)?
            .ok_or_else(|| missing("project deletion"))?;
        let runtime: ProjectId = super::parse(&self.runtime_project(deployment)?)?;
        // 1. A pending apply holds its branches. Ask it to stop and wait.
        let mut applying = 0;
        for mut apply in self.pending_applies()? {
            if apply.plan.context.deployment_id.to_string() == deployment {
                applying += 1;
                if !apply.cancel_requested {
                    apply.cancel_requested = true;
                    self.save_apply(&apply)?;
                }
            }
        }
        if applying > 0 {
            return Ok(Some("a project apply to stop".into()));
        }
        // 2. Branches, leaves first: a parent goes once its children are gone.
        let branches = self.list_branches(runtime, true)?;
        let gone = |b: &super::BranchRecord| {
            b.endpoint.desired_state == DesiredState::Deleted && b.observed_revision == b.revision
        };
        let mut remaining = 0;
        for branch in branches.iter().filter(|b| !gone(b)) {
            remaining += 1;
            let leaf = branches
                .iter()
                .filter(|child| child.branch.parent_id == Some(branch.branch.id))
                .all(gone);
            if leaf && branch.endpoint.desired_state != DesiredState::Deleted {
                // A branch that cannot go yet (a held lease, for example) is
                // tried again on the next pass.
                self.submit(
                    runtime,
                    &format!(
                        "project-delete:{}:{}",
                        deletion.operation_id, branch.branch.id
                    ),
                    Mutation::ForceDelete {
                        branch_id: branch.branch.id,
                        expected_revision: branch.revision,
                    },
                )?;
            }
        }
        if remaining > 0 {
            return Ok(Some(format!("{remaining} branches to be removed")));
        }
        // 3. Files the server created for the project.
        let owned: String = self.db.query_row(
            "SELECT owned_paths FROM project_deletions WHERE deployment_id=?1",
            [deployment],
            |r| r.get(0),
        )?;
        for path in serde_json::from_str::<Vec<String>>(&owned)? {
            let path = std::path::Path::new(&path);
            // Checked again here: only ever remove beneath the data directory.
            if path.starts_with(self.root()) && path != self.root() {
                match std::fs::remove_dir_all(path) {
                    Ok(()) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e.into()),
                }
            }
        }
        self.db.execute(
            "UPDATE project_deletions SET state='succeeded',waiting_on=NULL WHERE deployment_id=?1",
            [deployment],
        )?;
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project::ProjectConfig;

    fn project(store: &mut Store, name: &str) -> (String, ProjectId) {
        let config = ProjectConfig {
            format_version: 1,
            id: ProjectId::new(),
            name: name.into(),
        };
        store.register_project(&config).unwrap();
        let deployment: String = store
            .db
            .query_row(
                "SELECT id FROM deployments WHERE runtime_project_id=?1",
                [config.id.to_string()],
                |r| r.get(0),
            )
            .unwrap();
        (deployment, config.id)
    }

    #[test]
    fn anything_a_delete_would_not_reclaim_blocks_it_and_nothing_changes() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("state")).unwrap();
        let (deployment, runtime) = project(&mut store, "blocked");
        store
            .db
            .execute_batch(&format!(
                "PRAGMA foreign_keys=OFF;
                 INSERT INTO sync_policies VALUES ('policy','{runtime}','branch','paused','{{}}');
                 INSERT INTO sync_policies VALUES ('old-policy','{runtime}','branch','deleted','{{}}');
                 INSERT INTO analytical_artifacts(id,project_id,branch_id,kind) VALUES ('kept','{runtime}','branch','snapshot'),('collected','{runtime}','branch','snapshot');
                 INSERT INTO analytics_gc VALUES ('collected',NULL,'done');
                 PRAGMA foreign_keys=ON;"
            ))
            .unwrap();
        let impact = store.project_deletion_impact(&deployment).unwrap();
        assert_eq!(
            impact.blockers,
            [
                Blocker {
                    reason: "sync_pipelines",
                    count: 1
                },
                Blocker {
                    reason: "analytical_data",
                    count: 1
                }
            ]
        );
        let revision = store.project_record(&deployment).unwrap().revision;
        let error = store
            .delete_project(&deployment, revision, "operation")
            .unwrap_err()
            .to_string();
        assert!(error.contains("delete_blocked"), "{error}");
        let after = store.project_record(&deployment).unwrap();
        assert_eq!((after.name.as_str(), after.revision), ("blocked", revision));
        assert!(store.project_deletion(&deployment).unwrap().is_none());
    }

    #[test]
    fn a_delete_is_refused_on_a_stale_revision_and_repeats_only_for_its_own_operation() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("state")).unwrap();
        let (deployment, _) = project(&mut store, "going");
        let revision = store.project_record(&deployment).unwrap().revision;
        assert!(
            store
                .delete_project(&deployment, revision + 1, "first")
                .is_err()
        );
        store
            .delete_project(&deployment, revision, "first")
            .unwrap();
        store
            .delete_project(&deployment, revision, "first")
            .unwrap();
        assert!(
            store
                .delete_project(&deployment, revision, "second")
                .is_err()
        );
        assert!(store.project_record(&deployment).is_err());
        assert!(store.project_name_free("going", None).unwrap());
        // With no branches left there is nothing to wait for.
        store.advance_project_deletions().unwrap();
        assert_eq!(
            store.project_deletion(&deployment).unwrap().unwrap().state,
            "succeeded"
        );
    }
}
