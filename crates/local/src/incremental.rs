//! SY03 local-owner incremental epoch primitives; no automatic product policy.
use serde::{Deserialize, Serialize};
use supabricks_core::resource::{BranchId, EpochId, OperationId, ProjectId};

/// Explicit disk admission; does not enlarge memory, deadlines or metadata limits.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StorageProfile {
    #[default]
    Compact,
    Large,
}
impl StorageProfile {
    pub fn is_compact(&self) -> bool {
        *self == Self::Compact
    }
    pub fn generation_bytes(self) -> u64 {
        match self {
            Self::Compact => 1024 * 1024 * 1024,
            Self::Large => 128 * 1024 * 1024 * 1024,
        }
    }
    pub(crate) fn from_manifest(manifest: &serde_json::Value) -> crate::store::Result<Self> {
        match manifest.get("storage_profile") {
            None => Ok(Self::Compact),
            Some(value) => serde_json::from_value(value.clone())
                .map_err(|_| crate::store::error::invalid("invalid incremental storage profile")),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Run {
    pub id: OperationId,
    pub capture_id: OperationId,
    #[serde(default)]
    pub storage_generation: Option<OperationId>,
    #[serde(default, skip_serializing_if = "StorageProfile::is_compact")]
    pub storage_profile: StorageProfile,
    #[serde(default)]
    pub sync_run_id: Option<OperationId>,
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
    #[serde(default)]
    pub journal_deferrals: u32,
    #[serde(default)]
    pub retry_at_ms: Option<i64>,
    #[serde(default)]
    pub journal_reads: Vec<JournalRead>,
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

/// Fixed, bounded operational counters; no source values or SQL text.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JournalRead {
    pub attempts: u32,
    pub busy: u32,
    pub wait_ms: u64,
    pub elapsed_ms: u64,
    pub outcome: String,
}

#[cfg(test)]
mod storage_profile_tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn storage_profile_is_explicit_and_legacy_defaults_stay_compact() {
        assert_eq!(
            StorageProfile::from_manifest(&json!({})).unwrap(),
            StorageProfile::Compact
        );
        assert_eq!(
            StorageProfile::from_manifest(&json!({"storage_profile":"large"}))
                .unwrap()
                .generation_bytes(),
            128 * 1024_u64.pow(3)
        );
        for value in [
            json!(null),
            json!(true),
            json!({"generation_bytes":u64::MAX}),
            json!("unbounded"),
        ] {
            assert!(StorageProfile::from_manifest(&json!({"storage_profile":value})).is_err());
        }
        let legacy = crate::sync::Config::default();
        let serialized = serde_json::to_value(&legacy).unwrap();
        assert!(serialized.get("storage_profile").is_none());
        assert_eq!(
            serde_json::from_value::<crate::sync::Config>(serialized).unwrap(),
            legacy
        );
        let mut large = legacy;
        large.storage_profile = StorageProfile::Large;
        assert!(large.validate().is_err());
        large.mode = "triggered".into();
        large.strategy = "incremental".into();
        large.validate().unwrap();
    }
}
