//! Databases as the platform API reports them. A database is a root branch
//! and the branches made from it.
use super::{BranchRecord, Result, Store, error::missing};
use rusqlite::OptionalExtension;
use std::collections::HashMap;
use supabricks_core::resource::{BranchId, DesiredState, ProjectId};

#[derive(Clone, Debug, PartialEq)]
pub struct DatabaseRecord {
    /// The root branch, which is also the database's first branch.
    pub id: BranchId,
    pub project: ProjectId,
    pub name: String,
    pub state: &'static str,
    pub pg_major: u32,
    pub created_at_ms: i64,
    /// Rises with every lifecycle change and every rename.
    pub revision: i64,
    /// The lifecycle revision alone, which fences state changes.
    pub lifecycle_revision: i64,
    /// Live branches in the tree, including the root.
    pub branches: i64,
    /// Backups of any branch in the tree that still exist.
    pub backups: i64,
    pub holds_default: bool,
    pub connections: i64,
}

/// A branch as the platform API reports it.
#[derive(Clone, Debug, PartialEq)]
pub struct BranchView {
    pub id: BranchId,
    pub project: ProjectId,
    /// The root of the tree the branch is in.
    pub database: BranchId,
    pub name: String,
    pub parent: Option<BranchId>,
    pub state: &'static str,
    /// Whether it is the project's default branch.
    pub is_default: bool,
    /// The point in its parent it started from, as stored JSON.
    pub from: Option<String>,
    pub created_at_ms: i64,
    pub expires_at_ms: Option<i64>,
    /// The backup it was made from, if it was restored from one.
    pub backup: Option<BranchId>,
    /// Live branches made from it or from a backup of it.
    pub children: i64,
    /// Backups of it that still exist.
    pub backups: i64,
    pub connections: i64,
    /// Rises with every lifecycle change, rename and change of expiry.
    pub revision: i64,
    pub lifecycle_revision: i64,
}

fn state(record: &BranchRecord) -> &'static str {
    // An expired branch is on its way out even before its deletion is recorded.
    if record.expired {
        return "deleting";
    }
    let converged = record.revision == record.observed_revision;
    match (record.endpoint.desired_state, converged) {
        (DesiredState::Running, true) => "running",
        (DesiredState::Running, false) if record.observed_revision == 0 => "provisioning",
        (DesiredState::Running, false) => "resuming",
        (DesiredState::Suspended, true) => "suspended",
        (DesiredState::Suspended, false) => "suspending",
        (DesiredState::Deleted, _) => "deleting",
    }
}
/// Whether the branch still exists: not deleted, or its files not yet removed.
fn present(record: &BranchRecord) -> bool {
    record.endpoint.desired_state != DesiredState::Deleted
        || record.revision != record.observed_revision
}

impl Store {
    /// The runtime project behind a project (deployment) ID.
    pub(crate) fn project_runtime(&self, deployment: &str) -> Result<ProjectId> {
        let id: Option<String> = self
            .db
            .query_row(
                "SELECT runtime_project_id FROM deployments WHERE id=?1",
                [deployment],
                |r| r.get(0),
            )
            .optional()?;
        id.and_then(|id| id.parse().ok())
            .ok_or_else(|| missing("project"))
    }
    /// When a branch was created and how often its name or expiry changed.
    fn branch_details(&self, id: BranchId) -> Result<(i64, i64)> {
        Ok(self
            .db
            .query_row(
                "SELECT created_at_ms,edits FROM branch_details WHERE branch_id=?1",
                [id.to_string()],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?
            .unwrap_or((0, 0)))
    }
    /// The root of each branch of a project that still exists, by branch.
    fn roots(
        &self,
        project: ProjectId,
    ) -> Result<(Vec<BranchRecord>, HashMap<BranchId, BranchId>)> {
        let branches: Vec<_> = self
            .list_branches(project, true)?
            .into_iter()
            .filter(present)
            .collect();
        let parents: HashMap<_, _> = branches
            .iter()
            .map(|b| (b.branch.id, b.branch.parent_id))
            .collect();
        let mut roots = HashMap::new();
        for branch in &branches {
            let mut at = branch.branch.id;
            // A parent that is gone cannot occur while a child exists; if the
            // catalog says otherwise the child is left out, not misfiled.
            let root = loop {
                match parents.get(&at) {
                    Some(Some(parent)) => at = *parent,
                    Some(None) => break Some(at),
                    None => break None,
                }
            };
            if let Some(root) = root {
                roots.insert(branch.branch.id, root);
            }
        }
        Ok((branches, roots))
    }
    /// The branch each backup of a project was taken from, by the record that
    /// holds the backup. Those records are not branches to the platform API.
    pub(super) fn holds(&self, project: ProjectId) -> Result<HashMap<BranchId, BranchId>> {
        let mut holds = HashMap::new();
        let mut query = self.db.prepare(
            "SELECT k.branch_id,k.source_id FROM backups k JOIN branches b ON b.id=k.branch_id WHERE b.project_id=?1",
        )?;
        let rows = query.query_map([project.to_string()], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?;
        for row in rows {
            let (hold, source) = row?;
            holds.insert(super::parse(&hold)?, super::parse(&source)?);
        }
        Ok(holds)
    }
    /// Every database of a project, ordered by name.
    pub fn database_records(&self, project: ProjectId) -> Result<Vec<DatabaseRecord>> {
        let (branches, roots) = self.roots(project)?;
        let holds = self.holds(project)?;
        let mut records = Vec::new();
        for root in branches.iter().filter(|b| b.branch.parent_id.is_none()) {
            let members: Vec<_> = branches
                .iter()
                .filter(|b| roots.get(&b.branch.id) == Some(&root.branch.id))
                .collect();
            let (created_at_ms, edits) = self.branch_details(root.branch.id)?;
            records.push(DatabaseRecord {
                id: root.branch.id,
                project,
                name: root.branch.name.clone(),
                state: state(root),
                pg_major: u16::from(root.endpoint.pg_major) as u32,
                created_at_ms,
                revision: root.revision + edits,
                lifecycle_revision: root.revision,
                branches: members
                    .iter()
                    .filter(|b| {
                        b.endpoint.desired_state != DesiredState::Deleted
                            && !holds.contains_key(&b.branch.id)
                    })
                    .count()
                    .max(1) as i64,
                backups: members
                    .iter()
                    .filter(|b| holds.contains_key(&b.branch.id))
                    .count() as i64,
                holds_default: members.iter().any(|b| b.is_default),
                connections: self.connection_count(root.branch.id)? as i64,
            });
        }
        records.sort_by(|a, b| (&a.name, a.id.to_string()).cmp(&(&b.name, b.id.to_string())));
        Ok(records)
    }
    pub fn database_record(&self, project: ProjectId, id: BranchId) -> Result<DatabaseRecord> {
        self.database_records(project)?
            .into_iter()
            .find(|record| record.id == id)
            .ok_or_else(|| missing("database in project"))
    }
    /// The branches of a database, its first included, ordered by name.
    pub fn branch_views(&self, project: ProjectId, database: BranchId) -> Result<Vec<BranchView>> {
        let (branches, roots) = self.roots(project)?;
        let holds = self.holds(project)?;
        // A branch restored from a backup stands under the branch the backup
        // was taken from, which exists for as long as the backup does.
        let shown = |parent: Option<BranchId>| parent.map(|id| *holds.get(&id).unwrap_or(&id));
        let mut views = Vec::new();
        for branch in branches.iter().filter(|b| {
            roots.get(&b.branch.id) == Some(&database) && !holds.contains_key(&b.branch.id)
        }) {
            let id = branch.branch.id;
            let (created_at_ms, edits) = self.branch_details(id)?;
            let from: Option<String> = self
                .db
                .query_row(
                    "SELECT point FROM branch_pins WHERE child_id=?1",
                    [id.to_string()],
                    |r| r.get(0),
                )
                .optional()?;
            views.push(BranchView {
                id,
                project,
                database,
                name: branch.branch.name.clone(),
                parent: shown(branch.branch.parent_id),
                backup: branch
                    .branch
                    .parent_id
                    .filter(|parent| holds.contains_key(parent)),
                state: state(branch),
                is_default: branch.is_default,
                from,
                created_at_ms,
                expires_at_ms: branch.expires_at_ms,
                children: branches
                    .iter()
                    .filter(|b| {
                        shown(b.branch.parent_id) == Some(id)
                            && !holds.contains_key(&b.branch.id)
                            && b.endpoint.desired_state != DesiredState::Deleted
                    })
                    .count() as i64,
                backups: branches
                    .iter()
                    .filter(|b| holds.get(&b.branch.id) == Some(&id))
                    .count() as i64,
                connections: self.connection_count(id)? as i64,
                revision: branch.revision + edits,
                lifecycle_revision: branch.revision,
            });
        }
        views.sort_by(|a, b| (&a.name, a.id.to_string()).cmp(&(&b.name, b.id.to_string())));
        Ok(views)
    }
    pub fn branch_view(
        &self,
        project: ProjectId,
        database: BranchId,
        id: BranchId,
    ) -> Result<BranchView> {
        self.branch_views(project, database)?
            .into_iter()
            .find(|view| view.id == id)
            .ok_or_else(|| missing("branch in database"))
    }
    /// The database a branch belongs to, if the branch still exists.
    pub fn database_of(&self, project: ProjectId, branch: BranchId) -> Result<Option<BranchId>> {
        // The record that holds a backup is not a branch of any database.
        if self.is_backup(branch)? {
            return Ok(None);
        }
        Ok(self.roots(project)?.1.get(&branch).copied())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::operations::{Mutation, Operation, Ports, Step};
    use crate::project::ProjectConfig;
    use serde_json::json;

    fn setup() -> (tempfile::TempDir, Store, ProjectId) {
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
        (root, store, project)
    }
    fn ports(n: u16) -> Ports {
        Ports {
            sql: n,
            external_http: n + 1,
            internal_http: n + 2,
        }
    }
    fn finish(store: &mut Store, operation: &Operation) {
        while let Some(ticket) = store.ticket(operation.id).unwrap() {
            if ticket.step == Step::CaptureSuspend {
                store
                    .capture_suspend_lsn(&ticket, "0/2000".parse().unwrap())
                    .unwrap();
            }
            store
                .checkpoint(&ticket, json!({"effect": ticket.idempotency_key()}))
                .unwrap();
        }
    }
    fn database(store: &mut Store, project: ProjectId, name: &str, port: u16) -> Operation {
        store
            .submit(
                project,
                name,
                Mutation::CreateDatabase {
                    name: name.into(),
                    ports: ports(port),
                },
            )
            .unwrap()
    }
    fn set(
        store: &mut Store,
        project: ProjectId,
        id: BranchId,
        key: &str,
        desired: DesiredState,
    ) -> Operation {
        let expected_revision = store.branch(id).unwrap().revision;
        store
            .submit(
                project,
                key,
                Mutation::SetState {
                    branch_id: id,
                    expected_revision,
                    desired,
                },
            )
            .unwrap()
    }

    #[test]
    fn a_database_is_a_root_branch_and_its_state_follows_the_lifecycle() {
        let (_root, mut store, project) = setup();
        let main = database(&mut store, project, "main", 5500);
        let state = |store: &Store| {
            store
                .database_record(project, main.branch_id)
                .unwrap()
                .state
        };
        assert_eq!(state(&store), "provisioning");
        finish(&mut store, &main);
        assert_eq!(state(&store), "running");
        let suspend = set(
            &mut store,
            project,
            main.branch_id,
            "s",
            DesiredState::Suspended,
        );
        assert_eq!(state(&store), "suspending");
        finish(&mut store, &suspend);
        assert_eq!(state(&store), "suspended");
        let resume = set(
            &mut store,
            project,
            main.branch_id,
            "r",
            DesiredState::Running,
        );
        assert_eq!(state(&store), "resuming");
        finish(&mut store, &resume);
        assert_eq!(state(&store), "running");
        let record = store.database_record(project, main.branch_id).unwrap();
        assert!(record.holds_default && record.branches == 1 && record.created_at_ms > 0);
        assert_eq!(record.pg_major, 17);
    }

    #[test]
    fn branches_count_toward_their_own_database_only() {
        let (_root, mut store, project) = setup();
        let main = database(&mut store, project, "main", 5500);
        finish(&mut store, &main);
        let other = database(&mut store, project, "other", 5510);
        finish(&mut store, &other);
        let child = store
            .submit(
                project,
                "child",
                Mutation::CreateBranch {
                    name: "child".into(),
                    parent_id: Some(other.branch_id),
                    ports: ports(5520),
                },
            )
            .unwrap();
        let records = store.database_records(project).unwrap();
        let counts: Vec<_> = records
            .iter()
            .map(|r| (r.name.as_str(), r.branches, r.holds_default))
            .collect();
        assert_eq!(counts, [("main", 1, true), ("other", 2, false)]);
        assert_eq!(
            store.database_of(project, child.branch_id).unwrap(),
            Some(other.branch_id)
        );
        assert_eq!(
            store.database_of(project, main.branch_id).unwrap(),
            Some(main.branch_id)
        );
        // A child is not a database, and another project's database is not found.
        assert!(store.database_record(project, child.branch_id).is_err());
        assert!(
            store
                .database_record(ProjectId::new(), main.branch_id)
                .is_err()
        );
    }

    #[test]
    fn the_revision_rises_with_a_rename_and_with_a_state_change() {
        let (_root, mut store, project) = setup();
        let main = database(&mut store, project, "main", 5500);
        finish(&mut store, &main);
        let read = |store: &Store| store.database_record(project, main.branch_id).unwrap();
        let first = read(&store);
        store
            .rename_branch(project, main.branch_id, "primary")
            .unwrap();
        let renamed = read(&store);
        assert_eq!(renamed.revision, first.revision + 1);
        assert_eq!(
            renamed.lifecycle_revision, first.lifecycle_revision,
            "a rename does not fence lifecycle work"
        );
        assert_eq!(renamed.state, "running");
        store
            .rename_branch(project, main.branch_id, "primary")
            .unwrap();
        assert_eq!(
            read(&store).revision,
            renamed.revision,
            "the same name is no change"
        );
        let suspend = set(
            &mut store,
            project,
            main.branch_id,
            "s",
            DesiredState::Suspended,
        );
        finish(&mut store, &suspend);
        assert_eq!(read(&store).revision, renamed.revision + 1);
    }

    #[test]
    fn a_deleted_database_is_listed_until_its_files_are_gone() {
        let (_root, mut store, project) = setup();
        let main = database(&mut store, project, "main", 5500);
        finish(&mut store, &main);
        let other = database(&mut store, project, "other", 5510);
        finish(&mut store, &other);
        let delete = set(
            &mut store,
            project,
            other.branch_id,
            "d",
            DesiredState::Deleted,
        );
        assert_eq!(
            store
                .database_record(project, other.branch_id)
                .unwrap()
                .state,
            "deleting"
        );
        finish(&mut store, &delete);
        assert!(store.database_record(project, other.branch_id).is_err());
        assert_eq!(store.database_records(project).unwrap().len(), 1);
    }
    #[test]
    fn a_branch_view_reports_its_place_in_the_tree_and_what_changed_it() {
        let (_root, mut store, project) = setup();
        let main = database(&mut store, project, "main", 5500);
        finish(&mut store, &main);
        let other = database(&mut store, project, "other", 5510);
        let child = store
            .submit(
                project,
                "child",
                Mutation::BranchFrom {
                    name: "child".into(),
                    parent_id: main.branch_id,
                    ports: ports(5520),
                    point: crate::operations::BranchPoint::Time {
                        timestamp: "2026-01-01T00:00:00Z".into(),
                    },
                    timeout_ms: 90_000,
                },
            )
            .unwrap();
        let views = store.branch_views(project, main.branch_id).unwrap();
        let seen: Vec<_> = views
            .iter()
            .map(|v| (v.name.as_str(), v.parent, v.children, v.is_default, v.state))
            .collect();
        assert_eq!(
            seen,
            [
                ("child", Some(main.branch_id), 0, false, "provisioning"),
                ("main", None, 1, true, "running"),
            ]
        );
        assert!(views[0].from.as_deref().unwrap().contains("2026-01-01"));
        assert_eq!(views[1].from, None);
        // A branch is found under its own database and no other.
        assert!(
            store
                .branch_view(project, other.branch_id, child.branch_id)
                .is_err()
        );
        let before = views[0].revision;
        store
            .submit(
                project,
                "ttl",
                Mutation::SetTtl {
                    branch_id: child.branch_id,
                    expected_revision: 1,
                    expires_at_ms: Some(i64::MAX),
                },
            )
            .unwrap();
        store
            .rename_branch(project, child.branch_id, "renamed")
            .unwrap();
        let view = store
            .branch_view(project, main.branch_id, child.branch_id)
            .unwrap();
        assert_eq!(view.revision, before + 2);
        assert_eq!(view.lifecycle_revision, 1, "neither moves the lifecycle");
        assert_eq!(view.expires_at_ms, Some(i64::MAX));
        store
            .db
            .execute(
                "UPDATE branches SET expires_at_ms=1 WHERE id=?1",
                [child.branch_id.to_string()],
            )
            .unwrap();
        store.mark_expired().unwrap();
        let view = store
            .branch_view(project, main.branch_id, child.branch_id)
            .unwrap();
        assert_eq!(
            view.state, "deleting",
            "an expired branch is on its way out"
        );
    }
}
