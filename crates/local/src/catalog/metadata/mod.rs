//! Project-owned metadata. These descriptors grant neither storage access nor a reader lease.
mod service;
mod sources;
#[cfg(test)]
mod tests;
use crate::store::{Result, error::invalid};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
pub use service::Service;
use supabricks_core::resource::{BranchId, DeploymentId, EpochId, OperationId, ProjectId};

pub const VERSION: u32 = 1;
pub const MAX_BYTES: usize = 2 * 1024 * 1024;
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Capabilities {},
    Health {},
    Namespace {},
    EnsureNamespace {},
    List {
        branch: Option<String>,
        #[serde(default)]
        after: Option<String>,
        #[serde(default = "page_size")]
        limit: usize,
    },
    Describe {
        id: OperationId,
    },
    Resolve {
        id: OperationId,
        expected_version: String,
    },
    ValidateSource {
        id: OperationId,
        expected_version: String,
    },
    Poll {
        id: OperationId,
    },
}
fn page_size() -> usize {
    50
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Namespace {
    pub deployment_id: DeploymentId,
    pub project_id: ProjectId,
    pub provider_id: String,
    pub metastore_id: String,
    pub catalog: String,
    pub schema: String,
    pub catalog_id: Option<String>,
    pub schema_id: Option<String>,
    pub state: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Column {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
    pub name: String,
    pub data_type: String,
    pub nullable: bool,
    pub ordinal: u32,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Asset {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comment: Option<String>,
    pub id: OperationId,
    pub project_id: ProjectId,
    pub deployment_id: DeploymentId,
    pub branch_id: BranchId,
    pub resource_key: String,
    pub provider_id: String,
    pub provider: String,
    pub kind: String,
    pub incarnation: String,
    pub uc_object_id: Option<String>,
    pub alias: String,
    pub schema: String,
    pub name: String,
    pub columns: Vec<Column>,
    pub version: String,
    pub publication_revision: Option<i64>,
    pub epoch_id: Option<EpochId>,
    pub source_revision: Option<i64>,
    pub observed_at_ms: i64,
    pub snapshot_at_ms: Option<i64>,
    pub state: String,
}
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Code {
    Unavailable,
    NotFound,
    IdentityChanged,
    NameCollision,
    AmbiguousMutation,
    InvalidResponse,
    LimitExceeded,
    SchemaDrift,
    QuotingCollision,
    StaleVersion,
}
#[derive(Clone, Debug, Serialize)]
pub struct Fault {
    pub code: Code,
    pub retryable: bool,
    pub message: &'static str,
}
impl Fault {
    pub fn new(code: Code, message: &'static str) -> Self {
        let retryable = matches!(code, Code::Unavailable);
        Self {
            code,
            retryable,
            message,
        }
    }
}
pub(super) type RemoteResult<T> = std::result::Result<T, Fault>;
pub(super) fn fingerprint(value: &impl Serialize) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(
        serde_json::to_vec(value).expect("serializable metadata"),
    ))
}
pub(super) fn check_version(s: &str) -> Result<()> {
    if s.len() != 64 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(invalid(
            "expected_version must be a SHA-256 metadata revision",
        ));
    }
    Ok(())
}
pub fn capabilities() -> Value {
    json!({"api_version":VERSION,"profile":"local_owner_metadata","providers":["postgres","supabricks_snapshot","oss_unity_catalog"],"limits":{"page_size":100,"response_bytes":MAX_BYTES,"workers":2,"retained_requests":32,"request_retention_seconds":300,"provider_request_ms":750,"provider_retries":0,"postgres_timeout_ms":10000,"tables":128,"columns_per_table":128},"namespace_creation":true,"uc_publication":true,"cross_project_bindings":true,"storage_access":false,"governed_multiuser":false})
}
