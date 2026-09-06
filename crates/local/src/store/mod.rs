mod branches;
pub(crate) mod error;
mod journal;
mod migrations;
mod native;
mod ownership;
mod work;
pub use error::{Error, Result};
pub use migrations::SCHEMA_VERSION;
pub use work::{Epoch, Lease, ProcessRecord, TableMapping};

use crate::{operations::Ports, project::ProjectConfig};
use error::{conflict, invalid, missing};
use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::{
    fs::File,
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use supabricks_core::resource::*;

/// The connection is private and not Sync. Daemon request handling owns this
/// single writer; workers get tickets, never a SQLite connection.
pub struct Store {
    db: Connection,
    generation: i64,
    root: ownership::DataRoot,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BranchRecord {
    pub branch: Branch,
    pub endpoint: Endpoint,
    pub revision: i64,
    pub observed_revision: i64,
    /// Released only after all deletion checkpoints complete.
    pub ports: Option<Ports>,
    pub expires_at_ms: Option<i64>,
    pub expired: bool,
    pub is_default: bool,
    pub timeline_created: bool,
}
// Deliberately no Debug/Serialize: callers must consciously handle credentials.
pub struct ConnectionTarget {
    pub branch_id: BranchId,
    pub endpoint_id: EndpointId,
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: String,
}
impl Store {
    pub fn open(path: &Path) -> Result<Self> {
        let root = ownership::DataRoot::acquire(path)?;
        let database = root.path.join("state.sqlite3");
        // A fresh journal cannot reconstruct credentials, leases or ownership
        // from engine files. Never implicitly initialize over surviving data.
        if (!database.exists() || database.metadata()?.len() == 0)
            && [
                "runtime.json",
                "storage.pk8",
                "storage.pub",
                "safekeeper",
                "launches",
                "objects",
                "pageserver",
                "computes",
            ]
            .iter()
            .any(|p| root.path.join(p).exists())
        {
            return Err(conflict(
                "local control state is missing; restore a complete stopped-cell backup before startup",
            ));
        }
        ownership::private_file(&database)?.sync_all()?;
        let mut db = Connection::open_with_flags(
            &database,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        db.busy_timeout(Duration::from_secs(5))?;
        db.pragma_update(None, "foreign_keys", true)?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "synchronous", "FULL")?;
        migrations::migrate(&mut db)?;
        let generation = db.query_row(
            "UPDATE owner SET generation = generation + 1 WHERE id = 1 RETURNING generation",
            [],
            |r| r.get(0),
        )?;
        File::open(&root.path)?.sync_all()?;
        Ok(Self {
            db,
            generation,
            root,
        })
    }
    pub fn root(&self) -> &Path {
        &self.root.path
    }
    pub fn generation(&self) -> i64 {
        self.generation
    }
    pub fn register_project(&mut self, config: &ProjectConfig) -> Result<()> {
        config.validate()?;
        self.db.execute("INSERT INTO projects(id,name) VALUES (?1,?2) ON CONFLICT(id) DO UPDATE SET name=excluded.name", params![config.id.to_string(), config.name])?;
        Ok(())
    }
    pub fn project(&self, id: ProjectId) -> Result<ProjectConfig> {
        let name = self
            .db
            .query_row(
                "SELECT name FROM projects WHERE id=?1",
                [id.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("project"))?;
        Ok(ProjectConfig {
            format_version: 1,
            id,
            name,
        })
    }
    pub fn branch(&self, id: BranchId) -> Result<BranchRecord> {
        branch(&self.db, id)
    }
    pub fn rename_branch(&mut self, project: ProjectId, id: BranchId, name: &str) -> Result<()> {
        let name = canonical_name(name)?;
        let record = self.branch(id)?;
        if record.branch.project_id != project
            || record.endpoint.desired_state == DesiredState::Deleted
        {
            return Err(missing("live branch in project"));
        }
        // Revisions fence lifecycle work. A label rename does not invalidate it.
        self.db
            .execute(
                "UPDATE branches SET name=?1 WHERE id=?2",
                params![name, id.to_string()],
            )
            .map_err(constraint)?;
        Ok(())
    }
    pub fn select_worktree(
        &mut self,
        directory: &Path,
        project: ProjectId,
        branch_id: BranchId,
    ) -> Result<()> {
        let config = ProjectConfig::read(directory)?;
        if config.id != project {
            return Err(conflict("worktree belongs to another project"));
        }
        let record = self.branch(branch_id)?;
        if record.branch.project_id != project
            || record.endpoint.desired_state == DesiredState::Deleted
        {
            return Err(missing("live branch in project"));
        }
        let path = canonical_path(directory)?;
        let current: Option<String> = self
            .db
            .query_row(
                "SELECT project_id FROM worktrees WHERE path=?1",
                [&path],
                |r| r.get(0),
            )
            .optional()?;
        if current.is_some_and(|id| id != project.to_string()) {
            return Err(conflict("worktree already registered to another project"));
        }
        self.db.execute("INSERT INTO worktrees VALUES (?1,?2,?3) ON CONFLICT(path) DO UPDATE SET branch_id=excluded.branch_id", params![path, project.to_string(), branch_id.to_string()])?;
        Ok(())
    }
    pub fn selected_branch(&self, directory: &Path, project: ProjectId) -> Result<BranchId> {
        let config = ProjectConfig::read(directory)?;
        if config.id != project {
            return Err(conflict("worktree belongs to another project"));
        }
        let id: String = self
            .db
            .query_row(
                "SELECT branch_id FROM worktrees WHERE path=?1 AND project_id=?2",
                params![canonical_path(directory)?, project.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("worktree selection"))?;
        parse(&id)
    }
    /// Explicit branch target: callers never inherit another checkout's selection.
    pub fn connection(&self, project: ProjectId, id: BranchId) -> Result<ConnectionTarget> {
        let record = self.branch(id)?;
        if record.branch.project_id != project {
            return Err(missing("branch in project"));
        }
        self.accepting_work(id)?;
        if record.endpoint.desired_state != DesiredState::Running
            || record.observed_revision != record.revision
        {
            return Err(supabricks_core::error::OperationError::Unavailable(
                "endpoint has not converged to running".into(),
            )
            .into());
        }
        let password = self.db.query_row(
            "SELECT password FROM app_credentials WHERE endpoint_id=?1",
            [record.endpoint.id.to_string()],
            |r| r.get(0),
        )?;
        Ok(ConnectionTarget {
            branch_id: id,
            endpoint_id: record.endpoint.id,
            host: "127.0.0.1".into(),
            port: record.ports.ok_or_else(|| missing("endpoint ports"))?.sql,
            username: "supabricks_owner".into(),
            password,
        })
    }
    /// Private credentials for the engine adapter, including before convergence.
    pub fn endpoint_password(&self, endpoint: EndpointId) -> Result<String> {
        self.db
            .query_row(
                "SELECT password FROM credentials WHERE endpoint_id=?1",
                [endpoint.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("endpoint credential"))
    }
}
fn branch(db: &Connection, id: BranchId) -> Result<BranchRecord> {
    let row = db.query_row("SELECT b.project_id,b.name,b.tenant_id,b.timeline_id,b.parent_id,b.revision,b.desired,b.observed_revision,e.id,e.pg_major FROM branches b JOIN endpoints e ON e.branch_id=b.id WHERE b.id=?1", [id.to_string()], |r| {
        Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,Option<String>>(4)?,r.get::<_,i64>(5)?,r.get::<_,String>(6)?,r.get::<_,i64>(7)?,r.get::<_,String>(8)?,r.get::<_,u16>(9)?))
    }).optional()?.ok_or_else(|| missing("branch"))?;
    let mut ports = db.prepare("SELECT role,port FROM ports WHERE endpoint_id=?1")?;
    let entries: std::collections::HashMap<String, u16> = ports
        .query_map([&row.8], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<std::result::Result<_, _>>()?;
    let ports = if entries.is_empty() {
        None
    } else {
        Some(Ports {
            sql: *entries
                .get("sql")
                .ok_or_else(|| invalid("missing SQL port"))?,
            external_http: *entries
                .get("external_http")
                .ok_or_else(|| invalid("missing external HTTP port"))?,
            internal_http: *entries
                .get("internal_http")
                .ok_or_else(|| invalid("missing internal HTTP port"))?,
        })
    };
    Ok(BranchRecord {
        branch: Branch {
            id,
            project_id: parse(&row.0)?,
            name: row.1,
            tenant_id: parse(&row.2)?,
            timeline_id: parse(&row.3)?,
            parent_id: row.4.as_deref().map(parse).transpose()?,
            ancestor_lsn: db.query_row("SELECT ancestor_lsn FROM branches WHERE id=?1", [id.to_string()], |r| r.get::<_,Option<String>>(0))?.as_deref().map(parse).transpose()?,
        },
        endpoint: Endpoint {
            id: parse(&row.8)?,
            branch_id: id,
            pg_major: PgMajor::try_from(row.9)
                .map_err(supabricks_core::error::OperationError::from)?,
            desired_state: serde_json::from_value(serde_json::json!(row.6))?,
        },
        revision: row.5,
        observed_revision: row.7,
        ports,
        expires_at_ms: db.query_row("SELECT expires_at_ms FROM branches WHERE id=?1", [id.to_string()], |r| r.get(0))?,
        expired: db.query_row("SELECT expired OR (expires_at_ms IS NOT NULL AND expires_at_ms<=?2) FROM branches WHERE id=?1", params![id.to_string(),now_ms()?], |r| r.get(0))?,
        timeline_created: db.query_row("SELECT timeline_created FROM branches WHERE id=?1",[id.to_string()],|r|r.get(0))?,
        is_default: db.prepare("SELECT 1 FROM project_defaults WHERE branch_id=?1")?.exists([id.to_string()])?,
    })
}
fn canonical_name(name: &str) -> Result<String> {
    Ok(
        supabricks_core::validation::valid_name(&serde_json::json!({"name":name}), "name")
            .map_err(supabricks_core::error::OperationError::from)?,
    )
}
fn canonical_path(path: &Path) -> Result<String> {
    path.canonicalize()?
        .into_os_string()
        .into_string()
        .map_err(|_| invalid("worktree path must be UTF-8"))
}
fn parse<T: std::str::FromStr>(s: &str) -> Result<T> {
    s.parse()
        .map_err(|_| invalid("invalid identity in local state"))
}
fn constraint(error: rusqlite::Error) -> Error {
    if error.sqlite_error_code() == Some(rusqlite::ErrorCode::ConstraintViolation) {
        conflict("resource name, port or identity already reserved")
    } else {
        error.into()
    }
}
fn now_ms() -> Result<i64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|d| d.as_millis().try_into().ok())
        .ok_or_else(|| invalid("system clock is out of range"))
}
