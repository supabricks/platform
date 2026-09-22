//! SY02 local-owner capture generations. No incremental publication capability yet.
use crate::{
    api::Binding,
    store::{Result, Store, error::invalid},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use supabricks_core::resource::{BranchId, OperationId, ProjectId};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    pub spool_bytes: u64,
    pub wal_bytes: u64,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            spool_bytes: 512 * 1024 * 1024,
            wal_bytes: 512 * 1024 * 1024,
        }
    }
}
impl Limits {
    pub fn validate(&self) -> Result<()> {
        if !(16 * 1024 * 1024..=512 * 1024 * 1024).contains(&self.spool_bytes)
            || !(32 * 1024 * 1024..=512 * 1024 * 1024).contains(&self.wal_bytes)
            || self.wal_bytes % (1024 * 1024) != 0
        {
            return Err(invalid(
                "capture requires 16–512 MiB spool and 32–512 MiB whole-MiB WAL budgets",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Capture {
    pub id: OperationId,
    pub policy_id: OperationId,
    pub policy_revision: i64,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub identity: Value,
    pub limits: Limits,
    pub desired: String,
    pub state: String,
    pub error: Option<String>,
    pub created_at_ms: i64,
    pub worker_generation: i64,
    pub bootstrap_id: Option<OperationId>,
    pub bootstrap_lsn: Option<String>,
    #[serde(default)]
    pub barrier: Option<Value>,
    pub observed_at_ms: Option<i64>,
    pub start_lsn: Option<String>,
    pub captured_lsn: Option<String>,
    pub source_lsn: Option<String>,
    pub retained_wal_bytes: Option<u64>,
    pub spool_bytes: Option<u64>,
    pub cleanup_complete: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Start {
        policy_id: OperationId,
        expected_revision: i64,
        key: String,
        #[serde(default)]
        limits: Limits,
    },
    Status {
        id: OperationId,
    },
    Pause {
        id: OperationId,
        key: String,
    },
    Resume {
        id: OperationId,
        key: String,
    },
    Delete {
        id: OperationId,
        key: String,
    },
}
impl Command {
    pub(crate) fn key(&self) -> Option<&str> {
        match self {
            Self::Start { key, .. }
            | Self::Pause { key, .. }
            | Self::Resume { key, .. }
            | Self::Delete { key, .. } => Some(key),
            _ => None,
        }
    }
}
pub fn handle(store: &mut Store, binding: &Binding, command: Command) -> Result<Value> {
    store.binding_context(binding)?;
    store.capture_command(binding.project_id, command)
}
