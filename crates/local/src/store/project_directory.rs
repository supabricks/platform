//! Projects as the platform API reports them. A project is one instance: a
//! deployment row, its runtime project and its own name.
use super::{
    Result, Store,
    error::{Error, conflict, missing},
};
use rusqlite::{OptionalExtension, params};
use serde::Serialize;

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct ProjectRecord {
    /// The instance (deployment) ID.
    pub id: String,
    pub name: String,
    pub description: String,
    /// The portable source this instance was created from.
    pub source_id: String,
    pub created_at_ms: i64,
    pub revision: i64,
    /// One bound worktree, for the local owner.
    pub path: Option<String>,
    pub databases: i64,
    pub branches: i64,
}

/// The project changed since the caller read it.
pub(super) fn revision_mismatch() -> Error {
    conflict("revision_mismatch: the project has changed; read it again and retry")
}
pub(super) fn name_taken(name: &str) -> Error {
    conflict(&format!(
        "name_taken: a project named {name} already exists; choose another name"
    ))
}

const SELECT: &str = "SELECT d.id,p.name,coalesce(x.description,''),d.definition_id,coalesce(x.created_at_ms,0),d.revision,
 (SELECT min(path) FROM worktree_bindings w WHERE w.deployment_id=d.id),
 (SELECT count(*) FROM branches b WHERE b.project_id=p.id AND b.desired!='deleted' AND b.parent_id IS NULL),
 (SELECT count(*) FROM branches b WHERE b.project_id=p.id AND b.desired!='deleted')
 FROM deployments d JOIN projects p ON p.id=d.runtime_project_id
 LEFT JOIN project_details x ON x.deployment_id=d.id";

fn record(row: &rusqlite::Row) -> rusqlite::Result<ProjectRecord> {
    Ok(ProjectRecord {
        id: row.get(0)?,
        name: row.get(1)?,
        description: row.get(2)?,
        source_id: row.get(3)?,
        created_at_ms: row.get(4)?,
        revision: row.get(5)?,
        path: row.get(6)?,
        databases: row.get(7)?,
        branches: row.get(8)?,
    })
}

impl Store {
    /// Whether `name` is unused, ignoring case and the runtime project `except`.
    pub(crate) fn project_name_free(&self, name: &str, except: Option<&str>) -> Result<bool> {
        Ok(!self
            .db
            .prepare("SELECT 1 FROM projects WHERE lower(name)=lower(?1) AND id IS NOT ?2")?
            .exists(params![name, except])?)
    }
    pub(super) fn record_project_created(&self, deployment: &str) -> Result<()> {
        self.db.execute(
            "INSERT OR IGNORE INTO project_details(deployment_id,created_at_ms) VALUES (?1,?2)",
            params![deployment, chrono::Utc::now().timestamp_millis()],
        )?;
        Ok(())
    }
    /// The console's own landing project is not a project anyone created.
    fn console_home(&self) -> String {
        self.root()
            .join("console-home")
            .to_string_lossy()
            .into_owned()
    }
    /// The principal's rank in a project, directly or through a group:
    /// 0 none, 1 viewer, 2 editor, 3 administrator.
    pub(crate) fn project_role(&self, principal: &str, deployment: &str) -> u8 {
        super::authorization::role(&self.db, principal, deployment).unwrap_or(0)
    }
    /// Whether the caller administers the installation, not just a project.
    pub(crate) fn installation_admin(&self, context: &crate::identity::Context) -> bool {
        super::governed_console::realm_admin(&self.db, context).is_ok()
    }
    /// Record a change a signed-in caller made through the API.
    pub(crate) fn audit_api_change(
        &self,
        context: &crate::identity::Context,
        deployment: &str,
        action: &str,
        key: &str,
    ) -> Result<()> {
        super::authorization::audit(
            &self.db,
            context,
            deployment,
            super::authorization::policy(&self.db, deployment)?,
            action,
            key,
            deployment,
            &context.actor_id,
        )
    }
    /// Projects ordered by name then ID, after `after`, whose name contains
    /// `contains` (ignoring case). With `visible_to`, only projects where that
    /// principal holds a role. Returns at most `limit`.
    pub fn project_records(
        &self,
        after: Option<(&str, &str)>,
        contains: Option<&str>,
        visible_to: Option<&str>,
        limit: usize,
    ) -> Result<Vec<ProjectRecord>> {
        let subjects = visible_to
            .map(|principal| super::authorization::subjects(&self.db, principal))
            .transpose()?
            .map(|subjects| serde_json::to_string(&subjects))
            .transpose()?;
        let (after_name, after_id) = after.unwrap_or(("", ""));
        let pattern = contains.map(|text| {
            let escaped = text
                .replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_");
            format!("%{escaped}%")
        });
        let sql = format!(
            "{SELECT} WHERE NOT EXISTS (SELECT 1 FROM worktree_bindings h WHERE h.deployment_id=d.id AND h.path=?1)
             AND x.deleted_at_ms IS NULL
             AND (?2 IS NULL OR p.name LIKE ?2 ESCAPE '\\')
             AND (lower(p.name),d.id) > (lower(?3),?4)
             AND (?6 IS NULL OR EXISTS (SELECT 1 FROM authorization_roles a WHERE a.deployment=d.id
                  AND a.subject IN (SELECT value FROM json_each(?6))))
             ORDER BY lower(p.name),d.id LIMIT ?5"
        );
        Ok(self
            .db
            .prepare(&sql)?
            .query_map(
                params![
                    self.console_home(),
                    pattern,
                    after_name,
                    after_id,
                    limit as i64,
                    subjects
                ],
                record,
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?)
    }
    /// Rename a project or change its description. `name` must already be
    /// canonical. A change raises the revision by one; no change leaves it.
    pub fn update_project(
        &mut self,
        id: &str,
        name: Option<&str>,
        description: Option<&str>,
        expected_revision: Option<i64>,
    ) -> Result<ProjectRecord> {
        let current = self.project_record(id)?;
        if expected_revision.is_some_and(|expected| expected != current.revision) {
            return Err(revision_mismatch());
        }
        let name = name.filter(|name| *name != current.name);
        let description = description.filter(|text| *text != current.description);
        if name.is_none() && description.is_none() {
            return Ok(current);
        }
        let tx = self.db.transaction()?;
        if let Some(name) = name {
            let runtime: String = tx.query_row(
                "SELECT runtime_project_id FROM deployments WHERE id=?1",
                [id],
                |r| r.get(0),
            )?;
            if tx
                .prepare("SELECT 1 FROM projects WHERE lower(name)=lower(?1) AND id!=?2")?
                .exists(params![name, runtime])?
            {
                return Err(name_taken(name));
            }
            tx.execute(
                "UPDATE projects SET name=?1 WHERE id=?2",
                params![name, runtime],
            )?;
        }
        if let Some(description) = description {
            tx.execute(
                "INSERT INTO project_details(deployment_id,description,created_at_ms) VALUES (?1,?2,?3)
                 ON CONFLICT(deployment_id) DO UPDATE SET description=excluded.description",
                params![id, description, chrono::Utc::now().timestamp_millis()],
            )?;
        }
        // Checked again inside the transaction, so two writers cannot both win.
        if tx.execute(
            "UPDATE deployments SET revision=revision+1 WHERE id=?1 AND revision=?2",
            params![id, current.revision],
        )? != 1
        {
            return Err(revision_mismatch());
        }
        tx.commit()?;
        self.project_record(id)
    }
    /// A project that exists. A deleted one is not found.
    pub fn project_record(&self, id: &str) -> Result<ProjectRecord> {
        let record = self.project_record_any(id)?;
        if self.project_deleted(id)? {
            return Err(missing("project"));
        }
        Ok(record)
    }
    /// A project whether or not it has been deleted, so the operation that
    /// deletes it can still be followed.
    pub fn project_record_any(&self, id: &str) -> Result<ProjectRecord> {
        let sql = format!(
            "{SELECT} WHERE d.id=?1 AND NOT EXISTS (SELECT 1 FROM worktree_bindings h WHERE h.deployment_id=d.id AND h.path=?2)"
        );
        self.db
            .query_row(&sql, params![id, self.console_home()], record)
            .optional()?
            .ok_or_else(|| missing("project"))
    }
}
