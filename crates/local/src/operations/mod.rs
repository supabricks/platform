//! Durable intent and worker tickets. Engine adapters implement the effects in P03/P04.
use serde::{Deserialize, Serialize};
use supabricks_core::lsn::Lsn;
use supabricks_core::resource::{BranchId, DesiredState, OperationId, ProjectId};

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum BranchPoint {
    #[default]
    Head,
    Lsn {
        lsn: Lsn,
    },
    Time {
        timestamp: String,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ports {
    pub sql: u16,
    pub external_http: u16,
    pub internal_http: u16,
}
impl Ports {
    pub(crate) fn valid(self) -> bool {
        self.sql != 0
            && self.external_http != 0
            && self.internal_http != 0
            && self.sql != self.external_http
            && self.sql != self.internal_http
            && self.external_http != self.internal_http
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Mutation {
    CreateDatabase {
        name: String,
        ports: Ports,
    },
    BranchFrom {
        name: String,
        parent_id: BranchId,
        ports: Ports,
        #[serde(default)]
        point: BranchPoint,
        #[serde(default = "branch_timeout")]
        timeout_ms: u64,
    },
    CreateBranch {
        name: String,
        parent_id: Option<BranchId>,
        ports: Ports,
    },
    SetState {
        branch_id: BranchId,
        expected_revision: i64,
        desired: DesiredState,
    },
    SetTtl {
        branch_id: BranchId,
        expected_revision: i64,
        expires_at_ms: Option<i64>,
    },
    SetDefault {
        branch_id: BranchId,
    },
    ForceDelete {
        branch_id: BranchId,
        expected_revision: i64,
    },
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Step {
    CaptureBranchPoint,
    EnsureTimeline,
    StartCompute,
    StopCompute,
    DeleteTimeline,
    DeleteLocalFiles,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Pending,
    Succeeded,
    Superseded,
    Failed,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Operation {
    pub id: OperationId,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub revision: i64,
    pub status: Status,
    pub steps: Vec<Step>,
    pub next_step: usize,
    pub results: Vec<serde_json::Value>,
    pub error: Option<serde_json::Value>,
}
/// No database transaction is held while a ticket is executing. A worker must
/// make its effect idempotent using resource identity and this stable step key.
/// Fencing a checkpoint does not stop an already running process: P03 must also
/// reconcile surviving owned processes before allowing replacement writers.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkTicket {
    pub operation_id: OperationId,
    pub branch_id: BranchId,
    pub revision: i64,
    pub generation: i64,
    pub step_index: usize,
    pub step: Step,
}
impl WorkTicket {
    pub fn idempotency_key(&self) -> String {
        format!("{}:{}", self.operation_id, self.step_index)
    }
}

pub fn branch_timeout() -> u64 {
    90_000
}
