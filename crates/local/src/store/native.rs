use super::{BranchRecord, Result, Store, error::conflict, parse};
use crate::supervisor::OwnedProcess;
use rusqlite::{OptionalExtension, params};

impl Store {
    pub fn branches(&self) -> Result<Vec<BranchRecord>> {
        let mut q = self.db.prepare("SELECT id FROM branches ORDER BY id")?;
        let ids = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        ids.iter().map(|id| self.branch(parse(id)?)).collect()
    }

    pub fn native_processes(&self) -> Result<Vec<OwnedProcess>> {
        let mut q = self
            .db
            .prepare("SELECT record_json FROM native_processes ORDER BY role")?;
        let records = q
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        records
            .iter()
            .map(|r| Ok(serde_json::from_str(r)?))
            .collect()
    }

    pub fn record_native_process(&mut self, record: &OwnedProcess) -> Result<()> {
        if record.root != self.root()
            || record.generation != self.generation()
            || record.pid <= 1
            || record.token.is_empty()
            || record.start_identity.is_empty()
        {
            return Err(conflict("invalid native process ownership"));
        }
        let endpoint = if let Some((id, revision)) = record.branch {
            let b = self.branch(id)?;
            if b.revision != revision
                || b.endpoint.desired_state != supabricks_core::resource::DesiredState::Running
            {
                return Err(conflict("launch belongs to a stale branch revision"));
            }
            Some(b.endpoint.id)
        } else {
            None
        };
        let tx = self.db.transaction()?;
        if let Some(json) = tx
            .query_row(
                "SELECT record_json FROM native_processes WHERE role=?1",
                [&record.role],
                |r| r.get::<_, String>(0),
            )
            .optional()?
        {
            if serde_json::from_str::<OwnedProcess>(&json)? == *record {
                return Ok(());
            }
            return Err(conflict(
                "reconcile surviving processes before launching a replacement",
            ));
        }
        tx.execute(
            "INSERT INTO native_processes VALUES (?1,?2)",
            params![record.role, serde_json::to_string(record)?],
        )?;
        if let Some(endpoint) = endpoint {
            tx.execute(
                "INSERT INTO processes VALUES (?1,?2,?3,?4,?5,?5,?6)",
                params![
                    endpoint.to_string(),
                    record.role,
                    record.generation,
                    record.branch.unwrap().1,
                    record.pid,
                    record.start_identity
                ],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// Caller verified the entire OS group stopped, while holding data-root ownership.
    pub fn forget_native_process(&mut self, record: &OwnedProcess) -> Result<()> {
        let tx = self.db.transaction()?;
        let current = tx
            .query_row(
                "SELECT record_json FROM native_processes WHERE role=?1",
                [&record.role],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        if let Some(current) = current {
            if serde_json::from_str::<OwnedProcess>(&current)? != *record {
                return Err(conflict("native process identity changed"));
            }
            tx.execute("DELETE FROM native_processes WHERE role=?1", [&record.role])?;
            tx.execute("DELETE FROM processes WHERE role=?1 AND generation=?2 AND pid=?3 AND start_identity=?4", params![record.role,record.generation,record.pid,record.start_identity])?;
        }
        tx.commit()?;
        Ok(())
    }
}
