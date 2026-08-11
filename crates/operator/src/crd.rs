//! The two M1 CRDs (RFC 012). Endpoint state folds into status — no third CRD.

use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

fn default_suspend_after() -> i64 {
    300
}

fn default_cu_limit() -> i64 {
    10
}

/// Compute priority (RFC 011 QoS classes, compute layer): under contention,
/// CPU divides by CFS weight (higher priority = larger request fraction), and
/// preemption/eviction takes Low first — which here is just a rude suspend.
#[derive(Deserialize, Serialize, Clone, Copy, Debug, Default, PartialEq, Eq, JsonSchema)]
pub enum Priority {
    High,
    #[default]
    Standard,
    Low,
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
    /// Compute ceiling in CU (1 CU = 0.1 core). Limits may oversubscribe the
    /// pool; suspended databases hold zero CU.
    #[serde(default = "default_cu_limit")]
    pub cu_limit: i64,
    #[serde(default)]
    pub priority: Priority,
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
    /// Idle seconds before suspend (branches sleep aggressively by default).
    #[serde(default = "default_suspend_after")]
    pub suspend_after_seconds: i64,
    /// Compute ceiling in CU (1 CU = 0.1 core). Limits may oversubscribe the
    /// pool; suspended databases hold zero CU.
    #[serde(default = "default_cu_limit")]
    pub cu_limit: i64,
    #[serde(default)]
    pub priority: Priority,
    /// Branch point: head-of-parent only in M1 (`at` reserved, RFC 012).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ttl_seconds: Option<i64>,
}

/// An existing Postgres — anywhere — under governance without migration
/// (RFC 010: enrolled class; M1-lite: reachability + inventory health).
#[derive(CustomResource, Deserialize, Serialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "sspc.io",
    version = "v1alpha1",
    kind = "EnrolledDatabase",
    namespaced,
    status = "EnrolledStatus",
    shortname = "edb",
    printcolumn = r#"{"name":"Phase","type":"string","jsonPath":".status.phase"}"#,
    printcolumn = r#"{"name":"Version","type":"string","jsonPath":".status.serverVersion"}"#,
    printcolumn = r#"{"name":"DBs","type":"integer","jsonPath":".status.databaseCount"}"#
)]
#[serde(rename_all = "camelCase")]
pub struct EnrolledDatabaseSpec {
    /// Postgres connection URI. RFC 010's friction budget: a read-only
    /// monitoring role is all it needs (`GRANT pg_monitor`).
    pub connection_uri: String,
}

#[derive(Deserialize, Serialize, Clone, Debug, Default, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct EnrolledStatus {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<EnrolledPhase>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub server_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub database_count: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total_size: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_checked: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

#[derive(Deserialize, Serialize, Clone, Copy, Debug, PartialEq, Eq, JsonSchema)]
pub enum EnrolledPhase {
    Reachable,
    Unreachable,
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
    pub suspended_at: Option<String>,
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
