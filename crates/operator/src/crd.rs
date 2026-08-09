//! The two M1 CRDs (RFC 012). Endpoint state folds into status — no third CRD.

use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

fn default_suspend_after() -> i64 {
    300
}

#[derive(CustomResource, Deserialize, Serialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "sspc.io",
    version = "v1alpha1",
    kind = "Database",
    namespaced,
    status = "EndpointStatus",
    shortname = "db",
    printcolumn = r#"{"name":"Phase","type":"string","jsonPath":".status.phase"}"#,
    printcolumn = r#"{"name":"Port","type":"integer","jsonPath":".status.nodePort"}"#
)]
#[serde(rename_all = "camelCase")]
pub struct DatabaseSpec {
    /// Idle seconds before suspend (enforced by the operator's idle loop, P3).
    #[serde(default = "default_suspend_after")]
    pub suspend_after_seconds: i64,
    /// Optional TTL; the reaper deletes the resource after this many seconds.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ttl_seconds: Option<i64>,
}

#[derive(CustomResource, Deserialize, Serialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "sspc.io",
    version = "v1alpha1",
    kind = "Branch",
    namespaced,
    status = "EndpointStatus",
    shortname = "br",
    printcolumn = r#"{"name":"Database","type":"string","jsonPath":".spec.database"}"#,
    printcolumn = r#"{"name":"Phase","type":"string","jsonPath":".status.phase"}"#,
    printcolumn = r#"{"name":"Port","type":"integer","jsonPath":".status.nodePort"}"#
)]
#[serde(rename_all = "camelCase")]
pub struct BranchSpec {
    /// Name of the parent Database (same namespace).
    pub database: String,
    /// Branch point: head-of-parent only in M1 (`at` reserved, RFC 012).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ttl_seconds: Option<i64>,
}

#[derive(Deserialize, Serialize, Clone, Debug, Default, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct EndpointStatus {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<Phase>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tenant_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeline_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_port: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub flush_lsn: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_activity: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

#[derive(Deserialize, Serialize, Clone, Copy, Debug, PartialEq, Eq, JsonSchema)]
pub enum Phase {
    Provisioning,
    Active,
    Suspended,
    Expired,
    Failed,
}
