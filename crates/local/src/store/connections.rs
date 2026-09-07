//! Socket-owned leases have no wall-clock expiry. Only the daemon owning the
//! actual socket can release them; a new owner clears them after fencing writers.
use super::{
    Result, Store,
    error::{conflict, missing},
    parse,
};
use crate::operations::{Mutation, WorkTicket};
use rusqlite::{OptionalExtension, params};
use supabricks_core::{lsn::Lsn, resource::*};
impl Store {
    pub fn connection_port(&self, branch: BranchId) -> Result<Option<u16>> {
        Ok(self
            .db
            .query_row(
                "SELECT port FROM connection_endpoints WHERE branch_id=?1",
                [branch.to_string()],
                |r| r.get(0),
            )
            .optional()?)
    }
    pub fn connection_ports(&self) -> Result<Vec<(BranchId, u16)>> {
        let rows = self
            .db
            .prepare("SELECT branch_id,port FROM connection_endpoints")?
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows.into_iter()
            .map(|(id, p)| Ok((parse(&id)?, p)))
            .collect()
    }
    pub fn reserve_connection_port(&mut self, branch: BranchId, port: u16) -> Result<()> {
        self.accepting_work(branch)?;
        if self
            .db
            .prepare("SELECT 1 FROM ports WHERE port=?1")?
            .exists([port])?
        {
            return Err(conflict("connection port is reserved for compute"));
        }
        self.db.execute(
            "INSERT INTO connection_endpoints VALUES (?1,?2)",
            params![branch.to_string(), port],
        )?;
        Ok(())
    }
    pub fn clear_connection_leases(&mut self) -> Result<()> {
        self.db.execute("DELETE FROM connection_leases", [])?;
        Ok(())
    }
    pub fn accept_connection(&mut self, branch: BranchId) -> Result<LeaseId> {
        self.accepting_work(branch)?;
        let id = LeaseId::new();
        self.db.execute(
            "INSERT INTO connection_leases VALUES (?1,?2,?3)",
            params![id.to_string(), branch.to_string(), self.generation],
        )?;
        Ok(id)
    }
    pub fn release_connection(&mut self, id: LeaseId) -> Result<()> {
        self.db.execute(
            "DELETE FROM connection_leases WHERE id=?1 AND generation=?2",
            params![id.to_string(), self.generation],
        )?;
        Ok(())
    }
    pub fn connection_count(&self, branch: BranchId) -> Result<usize> {
        Ok(self.db.query_row(
            "SELECT count(*) FROM connection_leases WHERE branch_id=?1",
            [branch.to_string()],
            |r| r.get::<_, u32>(0),
        )? as usize)
    }
    pub fn wake_for_connection(&mut self, branch: BranchId) -> Result<()> {
        let b = self.branch(branch)?;
        // Let an already committed suspend finish retirement before waking.
        if b.endpoint.desired_state == DesiredState::Suspended
            && b.revision == b.observed_revision
            && self.connection_count(branch)? > 0
        {
            self.submit(
                b.branch.project_id,
                &format!("internal:connection-wake:{branch}:{}", b.revision),
                Mutation::SetState {
                    branch_id: branch,
                    expected_revision: b.revision,
                    desired: DesiredState::Running,
                },
            )?;
        }
        Ok(())
    }
    pub fn capture_suspend_lsn(&mut self, ticket: &WorkTicket, lsn: Lsn) -> Result<()> {
        if ticket.step != crate::operations::Step::CaptureSuspend
            || self.ticket(ticket.operation_id)?.as_ref() != Some(ticket)
            || ticket.generation != self.generation
        {
            return Err(conflict("stale suspension boundary"));
        }
        self.db.execute(
            "UPDATE branches SET suspend_lsn=?1,suspend_revision=?2 WHERE id=?3",
            params![
                lsn.to_string(),
                ticket.revision,
                ticket.branch_id.to_string()
            ],
        )?;
        Ok(())
    }
    pub fn suspend_lsn(&self, branch: BranchId, revision: i64) -> Result<Option<Lsn>> {
        let value: Option<String> = self
            .db
            .query_row(
                "SELECT suspend_lsn FROM branches WHERE id=?1 AND suspend_revision=?2",
                params![branch.to_string(), revision],
                |r| r.get(0),
            )
            .optional()?
            .flatten();
        value.as_deref().map(parse).transpose()
    }
    pub fn stable_connection_json(
        &self,
        project: ProjectId,
        branch: BranchId,
    ) -> Result<serde_json::Value> {
        let b = self.branch_in_project(project, branch)?;
        self.accepting_work(branch)?;
        let port = self
            .connection_port(branch)?
            .ok_or_else(|| missing("branch listener"))?;
        let password = self.app_password(b.endpoint.id)?;
        // Generated credentials contain only UUID characters; no URI escaping required.
        Ok(
            serde_json::json!({"branch_id":branch,"endpoint_id":b.endpoint.id,"host":"127.0.0.1","port":port,"database":"postgres","username":"supabricks_owner","password":password,
            "uri":format!("postgresql://supabricks_owner:{password}@127.0.0.1:{port}/postgres?sslmode=prefer")}),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        operations::{Ports, Step},
        project::ProjectConfig,
    };
    fn fixture() -> (tempfile::TempDir, Store, ProjectId, BranchId) {
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
        let op = store
            .submit(
                project,
                "root",
                Mutation::CreateBranch {
                    name: "main".into(),
                    parent_id: None,
                    ports: Ports {
                        sql: 5400,
                        external_http: 5401,
                        internal_http: 5402,
                    },
                },
            )
            .unwrap();
        finish(&mut store, op.id);
        (root, store, project, op.branch_id)
    }
    fn finish(store: &mut Store, id: OperationId) {
        while let Some(ticket) = store.ticket(id).unwrap() {
            if ticket.step == Step::CaptureSuspend {
                store
                    .capture_suspend_lsn(&ticket, "0/2000".parse().unwrap())
                    .unwrap();
            }
            store.checkpoint(&ticket, serde_json::json!({})).unwrap();
        }
    }
    #[test]
    fn connection_leases_serialize_suspend_and_deduplicate_wake() {
        let (_root, mut store, project, branch) = fixture();
        let first = store.accept_connection(branch).unwrap();
        let suspend = Mutation::SetState {
            branch_id: branch,
            expected_revision: 1,
            desired: DesiredState::Suspended,
        };
        assert!(store.submit(project, "suspend", suspend.clone()).is_err());
        store.release_connection(first).unwrap();
        let op = store.submit(project, "suspend", suspend).unwrap();
        let first = store.accept_connection(branch).unwrap();
        let second = store.accept_connection(branch).unwrap();
        store.wake_for_connection(branch).unwrap();
        assert_eq!(store.branch(branch).unwrap().revision, 2);
        finish(&mut store, op.id);
        store.wake_for_connection(branch).unwrap();
        store.wake_for_connection(branch).unwrap();
        assert_eq!(store.pending().unwrap().len(), 1);
        assert_eq!(store.branch(branch).unwrap().revision, 3);
        assert_eq!(store.connection_count(branch).unwrap(), 2);
        store.release_connection(first).unwrap();
        store.release_connection(second).unwrap();
        assert_eq!(store.connection_count(branch).unwrap(), 0);
    }
    #[test]
    fn connection_expiry_and_owner_loss_do_not_leave_permanent_leases() {
        let (_root, mut store, _project, branch) = fixture();
        let old = store.accept_connection(branch).unwrap();
        store
            .db
            .execute(
                "UPDATE branches SET expires_at_ms=1 WHERE id=?1",
                [branch.to_string()],
            )
            .unwrap();
        assert!(store.accept_connection(branch).is_err());
        let path = store.root().to_owned();
        drop(store);
        let mut store = Store::open(&path).unwrap();
        store.release_connection(old).unwrap();
        assert_eq!(store.connection_count(branch).unwrap(), 1);
        store.clear_connection_leases().unwrap();
        assert_eq!(store.connection_count(branch).unwrap(), 0);
    }
    #[test]
    fn persisted_listener_conflicts_are_not_reallocated_on_restart() {
        let (_root, mut store, project, branch) = fixture();
        let blocker = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = blocker.local_addr().unwrap().port();
        store.reserve_connection_port(branch, port).unwrap();
        let before = store.stable_connection_json(project, branch).unwrap();
        assert!(
            crate::connections::Gateway::new(&mut store, std::time::Duration::from_secs(1))
                .is_err()
        );
        assert_eq!(store.connection_port(branch).unwrap(), Some(port));
        drop(blocker);
        let gateway =
            crate::connections::Gateway::new(&mut store, std::time::Duration::from_secs(1))
                .unwrap();
        assert!(std::net::TcpListener::bind(("127.0.0.1", port)).is_err());
        assert_eq!(
            store.stable_connection_json(project, branch).unwrap(),
            before
        );
        drop(gateway);
        std::net::TcpListener::bind(("127.0.0.1", port)).unwrap();
    }
}
