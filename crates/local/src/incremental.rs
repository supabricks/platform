//! SY03 local-owner incremental epoch primitives; no automatic product policy.
use serde::{Deserialize, Serialize};
use supabricks_core::resource::{BranchId, EpochId, OperationId, ProjectId};
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Run {
    pub id: OperationId,
    pub capture_id: OperationId,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub epoch_id: EpochId,
    pub previous_epoch: Option<EpochId>,
    pub expected_head: Option<EpochId>,
    pub source_revision: i64,
    pub after_lsn: String,
    pub target_lsn: String,
    pub applied_lsn: Option<String>,
    pub state: String,
    pub error: Option<String>,
    pub created_at_ms: i64,
    pub deadline_ms: i64,
    pub worker_generation: i64,
    pub started_at_ms: Option<i64>,
    pub attempts: u32,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Apply {
        capture_id: OperationId,
        key: String,
    },
    Status {
        id: OperationId,
    },
    Cancel {
        id: OperationId,
        key: String,
    },
}
pub(crate) fn lsn(s: &str) -> crate::store::Result<u64> {
    let (a, b) = s
        .split_once('/')
        .ok_or_else(|| crate::store::error::invalid("invalid LSN"))?;
    let a = u32::from_str_radix(a, 16).map_err(|_| crate::store::error::invalid("invalid LSN"))?;
    let b = u32::from_str_radix(b, 16).map_err(|_| crate::store::error::invalid("invalid LSN"))?;
    Ok((u64::from(a) << 32) | u64::from(b))
}
