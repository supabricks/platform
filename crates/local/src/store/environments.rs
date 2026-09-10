use super::*;
use crate::environments::{Generation, Operation};

impl Store {
    pub(crate) fn environment_operations(&self) -> Result<Vec<Operation>> {
        let mut q = self
            .db
            .prepare("SELECT record_json FROM environment_operations ORDER BY rowid")?;
        q.query_map([], |r| r.get::<_, String>(0))?
            .map(|s| Ok(serde_json::from_str(&s?)?))
            .collect()
    }
    pub(crate) fn environment_generations(&self) -> Result<Vec<Generation>> {
        let mut q = self
            .db
            .prepare("SELECT record_json FROM environment_generations ORDER BY rowid")?;
        q.query_map([], |r| r.get::<_, String>(0))?
            .map(|s| Ok(serde_json::from_str(&s?)?))
            .collect()
    }
    pub(crate) fn save_environment_operation(&self, o: &Operation) -> Result<()> {
        self.db.execute(
            "INSERT INTO environment_operations VALUES (?1,?2,?3,?4,?5,?6)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state,record_json=excluded.record_json",
            params![
                o.id.to_string(),
                o.project.to_string(),
                o.worktree.to_str(),
                o.key,
                o.state,
                serde_json::to_string(o)?
            ],
        )?;
        Ok(())
    }
    pub(crate) fn save_environment_generation(&self, g: &Generation) -> Result<()> {
        self.db.execute(
            "INSERT INTO environment_generations VALUES (?1,?2,?3,?4,?5)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state,record_json=excluded.record_json",
            params![
                g.id.to_string(),
                g.project.to_string(),
                g.worktree.to_str(),
                g.state,
                serde_json::to_string(g)?
            ],
        )?;
        Ok(())
    }
    pub(crate) fn publish_environment(&mut self, o: &Operation, g: &Generation) -> Result<()> {
        let tx = self.db.transaction()?;
        tx.execute("UPDATE environment_generations SET state='ready',record_json=?2 WHERE id=?1 AND state='building'",
            params![g.id.to_string(),serde_json::to_string(g)?])?;
        if tx.changes() != 1 {
            return Err(conflict("environment publication is no longer pending"));
        }
        tx.execute("UPDATE environment_operations SET state='ready',record_json=?2 WHERE id=?1 AND state IN ('preparing','verifying')",
            params![o.id.to_string(),serde_json::to_string(o)?])?;
        if tx.changes() != 1 {
            return Err(conflict("environment operation is no longer pending"));
        }
        tx.execute("INSERT INTO environment_active VALUES (?1,?2,?3) ON CONFLICT(project_id,worktree) DO UPDATE SET generation_id=excluded.generation_id",
            params![g.project.to_string(),g.worktree.to_str(),g.id.to_string()])?;
        tx.commit()?;
        Ok(())
    }
    pub(crate) fn active_environment(
        &self,
        project: ProjectId,
        worktree: &Path,
    ) -> Result<Option<OperationId>> {
        let id: Option<String> = self
            .db
            .query_row(
                "SELECT generation_id FROM environment_active WHERE project_id=?1 AND worktree=?2",
                params![project.to_string(), worktree.to_str()],
                |r| r.get(0),
            )
            .optional()?;
        id.map(|s| s.parse().map_err(|_| invalid("invalid environment ID")))
            .transpose()
    }
    pub(crate) fn invalidate_environment(&mut self, g: &Generation) -> Result<()> {
        let tx = self.db.transaction()?;
        tx.execute(
            "DELETE FROM environment_active WHERE generation_id=?1",
            [g.id.to_string()],
        )?;
        tx.execute(
            "UPDATE environment_generations SET state=?2,record_json=?3 WHERE id=?1",
            params![g.id.to_string(), g.state, serde_json::to_string(g)?],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub(crate) fn lease_environment(
        &mut self,
        id: OperationId,
        holder: &str,
    ) -> Result<OperationId> {
        let tx = self.db.transaction()?;
        let ready = tx.query_row(
            "SELECT state='ready' FROM environment_generations WHERE id=?1",
            [id.to_string()],
            |r| r.get::<_, bool>(0),
        )?;
        if !ready {
            return Err(conflict("environment is not ready for a lease"));
        }
        let lease = OperationId::new();
        tx.execute("INSERT INTO environment_leases VALUES (?1,?2,?3,?4) ON CONFLICT(generation_id,holder) DO NOTHING",
            params![lease.to_string(),id.to_string(),holder,self.generation])?;
        let lease: String = tx.query_row("SELECT id FROM environment_leases WHERE generation_id=?1 AND holder=?2 AND daemon_generation=?3",
            params![id.to_string(),holder,self.generation],|r|r.get(0))?;
        tx.commit()?;
        lease
            .parse()
            .map_err(|_| invalid("invalid environment lease"))
    }
    pub(crate) fn release_environment_lease(&self, id: OperationId) -> Result<()> {
        self.db.execute(
            "DELETE FROM environment_leases WHERE id=?1 AND daemon_generation=?2",
            params![id.to_string(), self.generation],
        )?;
        Ok(())
    }
    pub(crate) fn clear_environment_leases(&self) -> Result<()> {
        if self
            .native_processes()?
            .iter()
            .any(|p| p.role.starts_with("notebook-kernel-"))
        {
            return Err(conflict("notebook kernels still own environment leases"));
        }
        self.db.execute("DELETE FROM environment_leases", [])?;
        Ok(())
    }
    pub(crate) fn collect_environment(&mut self, g: &Generation) -> Result<bool> {
        let tx = self.db.transaction()?;
        let used = tx.prepare("SELECT 1 FROM environment_leases WHERE generation_id=?1 UNION ALL SELECT 1 FROM environment_active WHERE generation_id=?1")?.exists([g.id.to_string()])?;
        if used {
            return Ok(false);
        }
        tx.execute("UPDATE environment_generations SET state='deleting',record_json=?2 WHERE id=?1 AND state IN ('ready','invalid','deleting')",
            params![g.id.to_string(),serde_json::to_string(g)?])?;
        let changed = tx.changes() == 1;
        tx.commit()?;
        Ok(changed)
    }
}
