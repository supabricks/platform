use super::error::{conflict, invalid, missing};
use super::{Result, Store, branch, canonical_name, constraint, now_ms, parse};
use crate::operations::{BranchPoint, Mutation, Operation, Status, Step, WorkTicket};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;
use supabricks_core::resource::*;

impl Store {
    /// Intent, identities, credentials and reservations commit together before
    /// any worker can see the operation. Keys are scoped to a project.
    pub fn submit(
        &mut self,
        project: ProjectId,
        key: &str,
        mut request: Mutation,
    ) -> Result<Operation> {
        if key.is_empty() || key.len() > 256 {
            return Err(invalid("idempotency key must contain 1–256 bytes"));
        }
        if let Mutation::CreateBranch { name, ports, .. }
        | Mutation::CreateDatabase { name, ports }
        | Mutation::BranchFrom { name, ports, .. } = &mut request
        {
            *name = canonical_name(name)?;
            if !ports.valid() {
                return Err(invalid("ports must be distinct and nonzero"));
            }
        }
        if let Mutation::BranchFrom {
            point, timeout_ms, ..
        } = &request
        {
            super::branches::validate_point(point)?;
            if !(1000..=300_000).contains(timeout_ms) {
                return Err(invalid(
                    "branch timeout must be between 1000 and 300000 milliseconds",
                ));
            }
        }
        let request_json = serde_json::to_string(&request)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing: Option<(String, String)> = tx
            .query_row(
                "SELECT id,request FROM operations WHERE project_id=?1 AND request_key=?2",
                params![project.to_string(), key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if let Some((id, previous)) = existing {
            if previous != request_json {
                return Err(conflict("idempotency key reused with different parameters"));
            }
            return operation(&tx, parse(&id)?);
        }
        if !tx
            .prepare("SELECT 1 FROM projects WHERE id=?1")?
            .exists([project.to_string()])?
        {
            return Err(missing("project"));
        }
        let (branch_id, revision, steps) = match &request {
            Mutation::CreateBranch { .. }
            | Mutation::CreateDatabase { .. }
            | Mutation::BranchFrom { .. } => create_branch(&tx, project, &request)?,
            Mutation::SetDefault { branch_id } => {
                let record = branch(&tx, *branch_id)?;
                if record.branch.project_id != project
                    || record.endpoint.desired_state == DesiredState::Deleted
                {
                    return Err(missing("live branch in project"));
                }
                if record.expires_at_ms.is_some() || record.expired {
                    return Err(conflict("default branch cannot have a TTL"));
                }
                tx.execute("INSERT INTO project_defaults VALUES (?1,?2) ON CONFLICT(project_id) DO UPDATE SET branch_id=excluded.branch_id",params![project.to_string(),branch_id.to_string()])?;
                (*branch_id, record.revision, vec![])
            }
            Mutation::SetTtl {
                branch_id,
                expected_revision,
                expires_at_ms,
            } => {
                let record = branch(&tx, *branch_id)?;
                if record.branch.project_id != project {
                    return Err(missing("branch in project"));
                }
                if record.revision != *expected_revision
                    || record.endpoint.desired_state == DesiredState::Deleted
                    || record.expired
                {
                    return Err(conflict("branch does not accept TTL changes"));
                }
                if record.is_default {
                    return Err(conflict("default branch cannot have a TTL"));
                }
                if expires_at_ms.is_some_and(|t| t <= now_ms().unwrap_or(i64::MAX)) {
                    return Err(invalid(
                        "TTL must be a future Unix timestamp in milliseconds",
                    ));
                }
                tx.execute(
                    "UPDATE branches SET expires_at_ms=?1 WHERE id=?2",
                    params![expires_at_ms, branch_id.to_string()],
                )?;
                (*branch_id, record.revision, vec![])
            }
            Mutation::SetState {
                branch_id,
                expected_revision,
                desired: _,
            }
            | Mutation::ForceDelete {
                branch_id,
                expected_revision,
            } => {
                let forced = matches!(request, Mutation::ForceDelete { .. });
                let desired = match &request {
                    Mutation::SetState { desired, .. } => *desired,
                    _ => DesiredState::Deleted,
                };
                let record = branch(&tx, *branch_id)?;
                if record.branch.project_id != project {
                    return Err(missing("branch in project"));
                }
                if record.revision != *expected_revision {
                    return Err(conflict("stale resource revision"));
                }
                if record.endpoint.desired_state == DesiredState::Deleted {
                    return Err(conflict("deleted branches cannot be resumed"));
                }
                if desired != DesiredState::Deleted
                    && record.branch.parent_id.is_some()
                    && !record.timeline_created
                {
                    return Err(conflict(
                        "incomplete child branch must finish creation or be deleted",
                    ));
                }
                if desired == DesiredState::Running && record.expired {
                    return Err(conflict("expired branch is draining"));
                }
                if desired == DesiredState::Deleted && record.is_default && !forced {
                    return Err(conflict(
                        "default branch is protected; choose another default or explicitly force deletion",
                    ));
                }
                if tx
                    .prepare("SELECT 1 FROM branch_pins WHERE parent_id=?1 AND active=1")?
                    .exists([branch_id.to_string()])?
                {
                    return Err(conflict("branch has protected branch operations"));
                }
                if !forced
                    && desired != DesiredState::Running
                    && tx
                        .prepare("SELECT 1 FROM leases WHERE branch_id=?1 AND expires_at_ms>?2")?
                        .exists(params![branch_id.to_string(), now_ms()?])?
                {
                    return Err(conflict("branch has active work leases"));
                }
                if desired == DesiredState::Deleted && tx.prepare("SELECT 1 FROM branches WHERE parent_id=?1 AND (desired!='deleted' OR observed_revision!=revision)")?.exists([branch_id.to_string()])? {
                    return Err(conflict("delete child branches before their parent"));
                }
                let revision = record
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| conflict("resource revision exhausted"))?;
                if forced {
                    tx.execute(
                        "DELETE FROM leases WHERE branch_id=?1",
                        [branch_id.to_string()],
                    )?;
                    tx.execute(
                        "DELETE FROM project_defaults WHERE branch_id=?1",
                        [branch_id.to_string()],
                    )?;
                }
                let state = serde_json::to_value(desired)?;
                tx.execute(
                    "UPDATE branches SET desired=?1,revision=?2 WHERE id=?3",
                    params![state.as_str(), revision, branch_id.to_string()],
                )?;
                tx.execute("UPDATE operations SET status='superseded' WHERE branch_id=?1 AND status='pending'", [branch_id.to_string()])?;
                let steps = match desired {
                    DesiredState::Running => vec![Step::EnsureTimeline, Step::StartCompute],
                    DesiredState::Suspended => vec![Step::StopCompute],
                    DesiredState::Deleted => vec![
                        Step::StopCompute,
                        Step::DeleteTimeline,
                        Step::DeleteLocalFiles,
                    ],
                };
                (*branch_id, revision, steps)
            }
        };
        let id = OperationId::new();
        tx.execute("INSERT INTO operations(id,project_id,request_key,request,branch_id,revision,steps) VALUES (?1,?2,?3,?4,?5,?6,?7)", params![id.to_string(),project.to_string(),key,request_json,branch_id.to_string(),revision,serde_json::to_string(&steps)?])?;
        if steps.is_empty() {
            tx.execute(
                "UPDATE operations SET status='succeeded' WHERE id=?1",
                [id.to_string()],
            )?;
        }
        let result = operation(&tx, id)?;
        tx.commit()?;
        Ok(result)
    }
    pub fn operation(&self, id: OperationId) -> Result<Operation> {
        operation(&self.db, id)
    }
    pub fn pending(&self) -> Result<Vec<Operation>> {
        let mut query = self
            .db
            .prepare("SELECT id FROM operations WHERE status='pending' AND NOT EXISTS (SELECT 1 FROM operation_errors WHERE operation_id=operations.id AND terminal=1) ORDER BY rowid")?;
        let ids = query
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        ids.iter().map(|id| self.operation(parse(id)?)).collect()
    }
    /// At-least-once delivery. Reissuing a ticket after a crash preserves the
    /// step key; the engine adapter must reconcile the same resource, not create
    /// a new one. No SQLite transaction escapes this call.
    pub fn ticket(&self, id: OperationId) -> Result<Option<WorkTicket>> {
        let op = self.operation(id)?;
        if op.status != Status::Pending {
            return Ok(None);
        }
        if self.branch(op.branch_id)?.revision != op.revision {
            return Err(conflict("operation revision is stale"));
        }
        let step = *op
            .steps
            .get(op.next_step)
            .ok_or_else(|| invalid("invalid operation checkpoint"))?;
        Ok(Some(WorkTicket {
            operation_id: id,
            branch_id: op.branch_id,
            revision: op.revision,
            generation: self.generation,
            step_index: op.next_step,
            step,
        }))
    }
    /// Only the current owner may acknowledge the current resource revision.
    /// A stale worker must reconcile/clean up its external effects via P03;
    /// accepting its result cannot revive suspended or deleted resources.
    pub fn checkpoint(&mut self, ticket: &WorkTicket, result: Value) -> Result<Operation> {
        if ticket.generation != self.generation {
            return Err(conflict("worker belongs to a previous daemon generation"));
        }
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let op = operation(&tx, ticket.operation_id)?;
        let record = branch(&tx, op.branch_id)?;
        if op.branch_id != ticket.branch_id
            || op.revision != ticket.revision
            || record.revision != ticket.revision
            || matches!(op.status, Status::Superseded | Status::Failed)
            || op.steps.get(ticket.step_index) != Some(&ticket.step)
        {
            return Err(conflict("stale worker result"));
        }
        if ticket.step_index < op.next_step {
            if op.results.get(ticket.step_index) != Some(&result) {
                return Err(conflict("checkpoint replay has different result"));
            }
            return Ok(op);
        }
        if ticket.step_index != op.next_step || op.status != Status::Pending {
            return Err(conflict("checkpoint is out of order"));
        }
        if ticket.step == Step::EnsureTimeline {
            tx.execute(
                "UPDATE branches SET timeline_created=1 WHERE id=?1",
                [op.branch_id.to_string()],
            )?;
        }
        let next = op.next_step + 1;
        tx.execute(
            "DELETE FROM operation_errors WHERE operation_id=?1",
            [op.id.to_string()],
        )?;
        if matches!(
            ticket.step,
            Step::StopCompute | Step::DeleteTimeline | Step::DeleteLocalFiles
        ) && tx
            .prepare("SELECT 1 FROM processes WHERE endpoint_id=?1")?
            .exists([record.endpoint.id.to_string()])?
        {
            return Err(conflict(
                "owned compute processes must be reconciled before recording shutdown",
            ));
        }
        tx.execute(
            "INSERT INTO checkpoints VALUES (?1,?2,?3)",
            params![
                op.id.to_string(),
                ticket.step_index as i64,
                serde_json::to_string(&result)?
            ],
        )?;
        tx.execute(
            "UPDATE operations SET next_step=?1,status=?2 WHERE id=?3",
            params![
                next as i64,
                if next == op.steps.len() {
                    "succeeded"
                } else {
                    "pending"
                },
                op.id.to_string()
            ],
        )?;
        if next == op.steps.len() {
            tx.execute(
                "UPDATE branches SET observed_revision=?1 WHERE id=?2",
                params![op.revision, op.branch_id.to_string()],
            )?;
            if record.endpoint.desired_state == DesiredState::Deleted {
                tx.execute(
                    "DELETE FROM ports WHERE endpoint_id=?1",
                    [record.endpoint.id.to_string()],
                )?;
                tx.execute(
                    "DELETE FROM credentials WHERE endpoint_id=?1",
                    [record.endpoint.id.to_string()],
                )?;
                tx.execute(
                    "DELETE FROM app_credentials WHERE endpoint_id=?1",
                    [record.endpoint.id.to_string()],
                )?;
                tx.execute(
                    "DELETE FROM worktrees WHERE branch_id=?1",
                    [op.branch_id.to_string()],
                )?;
            }
        }
        let updated = operation(&tx, op.id)?;
        tx.commit()?;
        Ok(updated)
    }
}
fn operation(db: &Connection, id: OperationId) -> Result<Operation> {
    let row = db.query_row("SELECT project_id,branch_id,revision,status,steps,next_step FROM operations WHERE id=?1", [id.to_string()], |r| Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,i64>(2)?,r.get::<_,String>(3)?,r.get::<_,String>(4)?,r.get::<_,u32>(5)?))).optional()?.ok_or_else(|| missing("operation"))?;
    let mut query =
        db.prepare("SELECT result FROM checkpoints WHERE operation_id=?1 ORDER BY step")?;
    let rows = query
        .query_map([id.to_string()], |r| r.get::<_, String>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let results = rows
        .iter()
        .map(|s| serde_json::from_str(s))
        .collect::<std::result::Result<_, _>>()?;
    let error: Option<(String, bool)> = db
        .query_row(
            "SELECT detail,terminal FROM operation_errors WHERE operation_id=?1",
            [id.to_string()],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    Ok(Operation {
        id,
        project_id: parse(&row.0)?,
        branch_id: parse(&row.1)?,
        revision: row.2,
        status: if row.3 == "pending" && error.as_ref().is_some_and(|e| e.1) {
            Status::Failed
        } else {
            serde_json::from_value(serde_json::json!(row.3))?
        },
        steps: serde_json::from_str(&row.4)?,
        next_step: row.5 as usize,
        results,
        error: error.map(|e| serde_json::from_str(&e.0)).transpose()?,
    })
}

fn create_branch(
    tx: &Connection,
    project: ProjectId,
    request: &Mutation,
) -> Result<(BranchId, i64, Vec<Step>)> {
    let head = BranchPoint::Head;
    let (name, parent_id, ports, point, default, timeout_ms) = match request {
        Mutation::CreateBranch {
            name,
            parent_id,
            ports,
        } => (name, *parent_id, *ports, &head, false, 90_000),
        Mutation::CreateDatabase { name, ports } => (name, None, *ports, &head, true, 90_000),
        Mutation::BranchFrom {
            name,
            parent_id,
            ports,
            point,
            timeout_ms,
        } => (name, Some(*parent_id), *ports, point, false, *timeout_ms),
        _ => return Err(invalid("expected branch creation")),
    };
    if let Some(id) = parent_id {
        let parent = branch(tx, id)?;
        if parent.branch.project_id != project
            || parent.endpoint.desired_state == DesiredState::Deleted
        {
            return Err(missing("live parent in project"));
        }
        if parent.expired {
            return Err(conflict("expired parent is draining"));
        }
        if parent.revision != parent.observed_revision {
            return Err(conflict("parent operation has not converged"));
        }
    }
    let id = BranchId::new();
    let endpoint = EndpointId::new();
    tx.execute("INSERT INTO branches(id,project_id,name,tenant_id,timeline_id,parent_id,revision,desired) VALUES (?1,?2,?3,?4,?5,?6,1,'running')",params![id.to_string(),project.to_string(),name,project.to_string().replace('-',""),id.to_string().replace('-',""),parent_id.map(|p|p.to_string())]).map_err(constraint)?;
    tx.execute(
        "INSERT INTO endpoints VALUES (?1,?2,17)",
        params![endpoint.to_string(), id.to_string()],
    )?;
    for (role, port) in [
        ("sql", ports.sql),
        ("external_http", ports.external_http),
        ("internal_http", ports.internal_http),
    ] {
        tx.execute(
            "INSERT INTO ports VALUES (?1,?2,?3)",
            params![port, endpoint.to_string(), role],
        )
        .map_err(constraint)?;
    }
    tx.execute(
        "INSERT INTO credentials VALUES (?1,'cloud_admin',?2)",
        params![
            endpoint.to_string(),
            format!("{}{}", OperationId::new(), OperationId::new())
        ],
    )?;
    tx.execute(
        "INSERT INTO app_credentials VALUES (?1,?2)",
        params![
            endpoint.to_string(),
            format!("{}{}", OperationId::new(), OperationId::new())
        ],
    )?;
    if default {
        tx.execute(
            "INSERT INTO project_defaults VALUES (?1,?2) ON CONFLICT(project_id) DO NOTHING",
            params![project.to_string(), id.to_string()],
        )?;
    }
    let mut steps = vec![];
    if let Some(parent) = parent_id {
        tx.execute(
            "INSERT INTO branch_pins(child_id,parent_id,point,deadline_ms) VALUES (?1,?2,?3,?4)",
            params![
                id.to_string(),
                parent.to_string(),
                serde_json::to_string(point)?,
                now_ms()? + timeout_ms as i64
            ],
        )?;
        steps.push(Step::CaptureBranchPoint);
    }
    steps.extend([Step::EnsureTimeline, Step::StartCompute]);
    Ok((id, 1, steps))
}
