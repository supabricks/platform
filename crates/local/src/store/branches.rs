//! Branch boundaries, operation-owned parent pins, TTL admission and credentials.
use super::{
    Result, Store, branch,
    error::{conflict, invalid, missing},
    now_ms, parse,
};
use crate::operations::{BranchPoint, Mutation, Operation, Step, WorkTicket};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use supabricks_core::{lsn::Lsn, resource::*};

pub(super) fn validate_point(point: &BranchPoint) -> Result<()> {
    match point {
        BranchPoint::Lsn { lsn } => {
            let text = lsn.to_string();
            let low = u32::from_str_radix(text.split_once('/').unwrap().1, 16).unwrap();
            if text == "0/0" || low % 8 != 0 {
                return Err(invalid(
                    "branch LSN must be nonzero and aligned to a WAL record boundary",
                ));
            }
        }
        BranchPoint::Time { timestamp } => {
            let t = chrono::DateTime::parse_from_rfc3339(timestamp)
                .map_err(|_| invalid("branch time must be RFC3339"))?;
            if t > chrono::Utc::now() {
                return Err(invalid("branch time must not be in the future"));
            }
        }
        BranchPoint::Head => {}
    }
    Ok(())
}
impl Store {
    pub fn accepting_work(&self, id: BranchId) -> Result<()> {
        let b = self.branch(id)?;
        if b.expired || b.endpoint.desired_state == DesiredState::Deleted {
            return Err(conflict("branch is not accepting new work"));
        }
        Ok(())
    }
    pub fn app_password(&self, endpoint: EndpointId) -> Result<String> {
        self.db
            .query_row(
                "SELECT password FROM app_credentials WHERE endpoint_id=?1",
                [endpoint.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("application credential"))
    }
    pub fn list_branches(
        &self,
        project: ProjectId,
        include_deleted: bool,
    ) -> Result<Vec<super::BranchRecord>> {
        self.project(project)?;
        Ok(self
            .branches()?
            .into_iter()
            .filter(|b| {
                b.branch.project_id == project
                    && (include_deleted || b.endpoint.desired_state != DesiredState::Deleted)
            })
            .collect())
    }
    pub fn pin_request(&self, child: BranchId) -> Result<(BranchPoint, i64)> {
        let (point, deadline): (String, i64) = self.db.query_row(
            "SELECT point,deadline_ms FROM branch_pins WHERE child_id=?1",
            [child.to_string()],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        Ok((serde_json::from_str(&point)?, deadline))
    }
    /// Persist the exact point before timeline creation. A retry never recaptures head.
    pub fn pin_lsn(&mut self, ticket: &WorkTicket, lsn: Lsn) -> Result<()> {
        if self.ticket(ticket.operation_id)?.as_ref() != Some(ticket)
            || ticket.generation != self.generation
        {
            return Err(conflict("stale boundary capture"));
        }
        if let Some(old) = self.branch(ticket.branch_id)?.branch.ancestor_lsn {
            return if old == lsn {
                Ok(())
            } else {
                Err(conflict("branch boundary is immutable"))
            };
        }
        self.db.execute(
            "UPDATE branches SET ancestor_lsn=?1 WHERE id=?2",
            params![lsn.to_string(), ticket.branch_id.to_string()],
        )?;
        Ok(())
    }
    pub fn operation_error(
        &mut self,
        id: OperationId,
        detail: Value,
        terminal: bool,
    ) -> Result<()> {
        self.db.execute("INSERT INTO operation_errors VALUES (?1,?2,?3) ON CONFLICT(operation_id) DO UPDATE SET detail=excluded.detail,terminal=excluded.terminal",params![id.to_string(),serde_json::to_string(&detail)?,terminal])?;
        Ok(())
    }
    pub fn ensure_parent_running(&mut self, child: BranchId) -> Result<super::BranchRecord> {
        let parent = self
            .branch(child)?
            .branch
            .parent_id
            .ok_or_else(|| invalid("root has no parent"))?;
        let b = self.branch(parent)?;
        if b.endpoint.desired_state == DesiredState::Deleted {
            return Err(conflict("parent was deleted"));
        }
        if b.endpoint.desired_state == DesiredState::Suspended {
            let tx = self.db.transaction()?;
            let revision = b
                .revision
                .checked_add(1)
                .ok_or_else(|| conflict("resource revision exhausted"))?;
            tx.execute(
                "UPDATE branches SET desired='running',revision=?1 WHERE id=?2",
                params![revision, parent.to_string()],
            )?;
            let id = OperationId::new();
            tx.execute("INSERT INTO operations(id,project_id,request_key,request,branch_id,revision,steps) VALUES (?1,?2,?3,?4,?5,?6,?7)",params![id.to_string(),b.branch.project_id.to_string(),format!("internal:wake:{id}"),serde_json::to_string(&Mutation::SetState{branch_id:parent,expected_revision:b.revision,desired:DesiredState::Running})?,parent.to_string(),revision,serde_json::to_string(&vec![Step::EnsureTimeline,Step::StartCompute])?])?;
            tx.execute(
                "INSERT INTO parent_wakes VALUES (?1,?2)",
                params![parent.to_string(), revision],
            )?;
            tx.commit()?;
        }
        self.branch(parent)
    }
    /// Pins are durable operation-owned leases, so owner crashes do not expire
    /// protection while a branch creation can still be replayed.
    pub fn reconcile_parent_pins(&mut self) -> Result<()> {
        self.db.execute("UPDATE branch_pins SET active=0 WHERE active=1 AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.branch_id=branch_pins.child_id AND o.status='pending' AND NOT EXISTS (SELECT 1 FROM operation_errors e WHERE e.operation_id=o.id AND e.terminal=1) AND o.next_step < 2 AND json_extract(o.steps,'$[0]')='capture_branch_point')",[])?;
        let rows = {
            let mut q=self.db.prepare("SELECT parent_id,revision FROM parent_wakes WHERE NOT EXISTS (SELECT 1 FROM branch_pins WHERE parent_id=parent_wakes.parent_id AND active=1)")?;
            q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        for (parent, revision) in rows {
            let id = parse(&parent)?;
            let b = self.branch(id)?;
            if b.revision == revision && b.endpoint.desired_state == DesiredState::Running {
                // Existing protected work may defer restoration. Never override
                // a later explicit lifecycle decision with a stale internal wake.
                if !self.leases(id)?.is_empty() {
                    continue;
                }
                self.submit(
                    b.branch.project_id,
                    &format!("internal:restore:{parent}:{revision}"),
                    Mutation::SetState {
                        branch_id: id,
                        expected_revision: revision,
                        desired: DesiredState::Suspended,
                    },
                )?;
            }
            self.db
                .execute("DELETE FROM parent_wakes WHERE parent_id=?1", [parent])?;
        }
        Ok(())
    }
    pub fn mark_expired(&mut self) -> Result<()> {
        self.db.execute(
            "UPDATE branches SET expired=1 WHERE expires_at_ms<=?1 AND desired!='deleted'",
            [now_ms()?],
        )?;
        Ok(())
    }
    pub fn expire_branches(&mut self, busy: &[BranchId]) -> Result<Vec<Operation>> {
        self.db.execute(
            "UPDATE branches SET expired=1 WHERE expires_at_ms<=?1 AND desired!='deleted'",
            [now_ms()?],
        )?;
        let mut operations = vec![];
        for b in self
            .branches()?
            .into_iter()
            .filter(|b| b.expired && b.endpoint.desired_state != DesiredState::Deleted)
        {
            if busy.contains(&b.branch.id) {
                continue;
            }
            match self.submit(
                b.branch.project_id,
                &format!("internal:ttl:{}:{}", b.branch.id, b.revision),
                Mutation::SetState {
                    branch_id: b.branch.id,
                    expected_revision: b.revision,
                    desired: DesiredState::Deleted,
                },
            ) {
                Ok(op) => operations.push(op),
                Err(super::Error::Operation(supabricks_core::error::OperationError::Conflict(
                    _,
                ))) => {}
                Err(e) => return Err(e),
            }
        }
        Ok(operations)
    }
    pub fn branch_in_project(
        &self,
        project: ProjectId,
        id: BranchId,
    ) -> Result<super::BranchRecord> {
        let b = branch(&self.db, id)?;
        if b.branch.project_id != project {
            return Err(missing("branch in project"));
        }
        Ok(b)
    }
    pub fn connection_json(&self, project: ProjectId, id: BranchId) -> Result<Value> {
        let c = self.connection(project, id)?;
        Ok(
            json!({"branch_id":c.branch_id,"endpoint_id":c.endpoint_id,"host":c.host,"port":c.port,"database":"postgres","username":c.username,"password":c.password}),
        )
    }
}

impl Store {
    pub(crate) fn validation_tenants(&self) -> Result<std::collections::HashSet<String>> {
        Ok(self
            .db
            .prepare("SELECT DISTINCT tenant_id FROM branches")?
            .query_map([], |row| row.get(0))?
            .collect::<rusqlite::Result<_>>()?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        operations::{Ports, Status},
        project::ProjectConfig,
    };
    use std::time::Duration;
    fn setup() -> (tempfile::TempDir, Store, ProjectId) {
        let root = tempfile::tempdir().unwrap();
        let mut s = Store::open(&root.path().join("cell")).unwrap();
        let p = ProjectId::new();
        s.register_project(&ProjectConfig {
            format_version: 1,
            id: p,
            name: "project".into(),
        })
        .unwrap();
        (root, s, p)
    }
    fn ports(n: u16) -> Ports {
        Ports {
            sql: n,
            external_http: n + 1,
            internal_http: n + 2,
        }
    }
    fn finish(s: &mut Store, id: OperationId) {
        while let Some(t) = s.ticket(id).unwrap() {
            if t.step == Step::CaptureBranchPoint {
                s.pin_lsn(&t, "0/2000".parse().unwrap()).unwrap();
            }
            s.checkpoint(&t, json!({"effect":t.idempotency_key()}))
                .unwrap();
        }
    }
    fn root(s: &mut Store, p: ProjectId) -> Operation {
        let op = s
            .submit(
                p,
                "root",
                Mutation::CreateDatabase {
                    name: "main".into(),
                    ports: ports(5500),
                },
            )
            .unwrap();
        finish(s, op.id);
        op
    }
    #[test]
    fn exact_pins_survive_restart_and_failure_does_not_substitute_a_new_head() {
        let (_root, mut s, p) = setup();
        let parent = root(&mut s, p);
        let op = s
            .submit(
                p,
                "branch",
                Mutation::BranchFrom {
                    name: "child".into(),
                    parent_id: parent.branch_id,
                    ports: ports(5600),
                    point: BranchPoint::Head,
                    timeout_ms: 90_000,
                },
            )
            .unwrap();
        let ticket = s.ticket(op.id).unwrap().unwrap();
        let point = "0/2000".parse().unwrap();
        s.pin_lsn(&ticket, point).unwrap();
        let path = s.root().to_owned();
        drop(s);
        let mut s = Store::open(&path).unwrap();
        assert_eq!(
            s.branch(op.branch_id).unwrap().branch.ancestor_lsn,
            Some(point)
        );
        assert!(s.pin_lsn(&ticket, point).is_err());
        let ticket = s.ticket(op.id).unwrap().unwrap();
        assert!(s.pin_lsn(&ticket, "0/3000".parse().unwrap()).is_err());
        s.operation_error(op.id, json!({"code":"ingestion_timeout"}), true)
            .unwrap();
        assert_eq!(s.operation(op.id).unwrap().status, Status::Failed);
        assert!(s.ticket(op.id).unwrap().is_none());
        assert!(s.pending().unwrap().is_empty());
        assert_eq!(
            s.branch(op.branch_id).unwrap().branch.ancestor_lsn,
            Some(point)
        );
        assert!(!s.branch(op.branch_id).unwrap().timeline_created);
        s.reconcile_parent_pins().unwrap();
        assert!(
            s.submit(
                p,
                "parent-delete",
                Mutation::ForceDelete {
                    branch_id: parent.branch_id,
                    expected_revision: 1
                }
            )
            .is_err()
        );
        let del = s
            .submit(
                p,
                "cleanup",
                Mutation::SetState {
                    branch_id: op.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Deleted,
                },
            )
            .unwrap();
        finish(&mut s, del.id);
        assert!(s.branch(op.branch_id).unwrap().ports.is_none());
    }
    #[test]
    fn default_protection_and_ttl_drain_are_durable() {
        let (_root, mut s, p) = setup();
        let parent = root(&mut s, p);
        assert!(
            s.submit(
                p,
                "ttl-default",
                Mutation::SetTtl {
                    branch_id: parent.branch_id,
                    expected_revision: 1,
                    expires_at_ms: Some(now_ms().unwrap() + 60000)
                }
            )
            .is_err()
        );
        assert!(
            s.submit(
                p,
                "delete-default",
                Mutation::SetState {
                    branch_id: parent.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Deleted
                }
            )
            .is_err()
        );
        let op = s
            .submit(
                p,
                "child",
                Mutation::BranchFrom {
                    name: "temporary".into(),
                    parent_id: parent.branch_id,
                    ports: ports(5600),
                    point: BranchPoint::Head,
                    timeout_ms: 90_000,
                },
            )
            .unwrap();
        finish(&mut s, op.id);
        s.reconcile_parent_pins().unwrap();
        let lease = s
            .acquire_lease(op.branch_id, None, "existing", Duration::from_secs(60))
            .unwrap();
        s.submit(
            p,
            "ttl",
            Mutation::SetTtl {
                branch_id: op.branch_id,
                expected_revision: 1,
                expires_at_ms: Some(now_ms().unwrap() + 60000),
            },
        )
        .unwrap();
        // Advance only the fixture's expiry, without waiting a minute.
        s.db.execute(
            "UPDATE branches SET expires_at_ms=?1 WHERE id=?2",
            params![now_ms().unwrap() - 1, op.branch_id.to_string()],
        )
        .unwrap();
        assert!(s.expire_branches(&[]).unwrap().is_empty());
        assert!(s.branch(op.branch_id).unwrap().expired);
        assert!(s.connection(p, op.branch_id).is_err());
        assert!(
            s.acquire_lease(op.branch_id, None, "late", Duration::from_secs(60))
                .is_err()
        );
        let lease = s.renew_lease(&lease, Duration::from_secs(60)).unwrap();
        s.release_lease(&lease).unwrap();
        assert!(s.expire_branches(&[op.branch_id]).unwrap().is_empty());
        let deletes = s.expire_branches(&[]).unwrap();
        assert_eq!(deletes.len(), 1);
        finish(&mut s, deletes[0].id);
        let b = s.branch(op.branch_id).unwrap();
        assert!(b.ports.is_none());
        assert!(s.app_password(b.endpoint.id).is_err());
        assert!(s.branch(parent.branch_id).unwrap().is_default);
    }
    #[test]
    fn suspended_parent_wake_and_restore_survive_owner_loss() {
        let (_root, mut s, p) = setup();
        let parent = root(&mut s, p);
        let op = s
            .submit(
                p,
                "suspend",
                Mutation::SetState {
                    branch_id: parent.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Suspended,
                },
            )
            .unwrap();
        finish(&mut s, op.id);
        let child = s
            .submit(
                p,
                "child",
                Mutation::BranchFrom {
                    name: "child".into(),
                    parent_id: parent.branch_id,
                    ports: ports(5600),
                    point: BranchPoint::Head,
                    timeout_ms: 90_000,
                },
            )
            .unwrap();
        let w = s.ensure_parent_running(child.branch_id).unwrap();
        assert_eq!(w.endpoint.desired_state, DesiredState::Running);
        assert!(
            s.submit(
                p,
                "race",
                Mutation::SetState {
                    branch_id: parent.branch_id,
                    expected_revision: w.revision,
                    desired: DesiredState::Suspended
                }
            )
            .is_err()
        );
        let path = s.root().to_owned();
        drop(s);
        let mut s = Store::open(&path).unwrap();
        assert_eq!(
            s.ensure_parent_running(child.branch_id).unwrap().revision,
            w.revision
        );
        finish(&mut s, child.id);
        s.reconcile_parent_pins().unwrap();
        assert_eq!(
            s.branch(parent.branch_id).unwrap().endpoint.desired_state,
            DesiredState::Suspended
        );
        for op in s.pending().unwrap() {
            finish(&mut s, op.id);
        }
        assert_eq!(s.branch(parent.branch_id).unwrap().revision, w.revision + 1);
    }
    #[test]
    fn malformed_and_unaligned_positions_never_enter_the_journal() {
        let (_root, mut s, p) = setup();
        let parent = root(&mut s, p);
        for point in [
            BranchPoint::Lsn {
                lsn: "0/0".parse().unwrap(),
            },
            BranchPoint::Lsn {
                lsn: "0/11".parse().unwrap(),
            },
            BranchPoint::Time {
                timestamp: "tomorrow".into(),
            },
        ] {
            assert!(
                s.submit(
                    p,
                    "bad",
                    Mutation::BranchFrom {
                        name: "bad".into(),
                        parent_id: parent.branch_id,
                        ports: ports(5600),
                        point,
                        timeout_ms: 90_000,
                    }
                )
                .is_err()
            );
        }
        assert_eq!(s.branches().unwrap().len(), 1);
        assert!(s.pending().unwrap().is_empty());
    }
    #[test]
    fn empty_object_mount_allows_initialization_but_existing_objects_do_not() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join("objects")).unwrap();
        let store = Store::open(root.path()).unwrap();
        drop(store);
        std::fs::remove_file(root.path().join("state.sqlite3")).unwrap();
        std::fs::write(root.path().join("objects/acknowledged"), b"data").unwrap();
        assert!(Store::open(root.path()).is_err());
        assert!(!root.path().join("state.sqlite3").exists());
    }
    #[test]
    fn lost_control_state_is_not_silently_reconstructed_from_engine_files() {
        let (root, s, _) = setup();
        let path = s.root().to_owned();
        drop(s);
        std::fs::write(path.join("runtime.json"), "{}").unwrap();
        std::fs::rename(
            path.join("state.sqlite3"),
            root.path().join("saved.sqlite3"),
        )
        .unwrap();
        assert!(Store::open(&path).is_err());
        assert!(!path.join("state.sqlite3").exists());
        std::fs::rename(
            root.path().join("saved.sqlite3"),
            path.join("state.sqlite3"),
        )
        .unwrap();
        Store::open(&path).unwrap();
    }
}
