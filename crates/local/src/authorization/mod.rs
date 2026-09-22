//! One server-side authorization boundary. Shared runtime ingress stays gated.
pub mod inventory;
use crate::{
    identity::Channel,
    store::{Result, error::invalid},
};
use serde::{Deserialize, Serialize};
pub const VERSION: u32 = 1;
pub const CONTROL_SCOPE: &str = "project:control";
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Viewer,
    Editor,
    Administrator,
}
impl Role {
    pub(crate) fn name(&self) -> &'static str {
        match self {
            Self::Viewer => "viewer",
            Self::Editor => "editor",
            Self::Administrator => "administrator",
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(
    tag = "kind",
    content = "id",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum Subject {
    Principal(String),
    Group(String),
}
impl Subject {
    pub(crate) fn key(&self) -> String {
        match self {
            Self::Principal(id) => format!("principal:{id}"),
            Self::Group(id) => format!("group:{id}"),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Grant {
    Execute,
    StopAny,
    ActAs,
}
impl Grant {
    pub(crate) fn name(&self) -> &'static str {
        match self {
            Self::Execute => "execute",
            Self::StopAny => "stop_any",
            Self::ActAs => "act_as",
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Workspace {
        command: crate::console::governed::Command,
    },
    Data {
        deployment: String,
        command: crate::governed::Command,
    },
    Runtime {
        deployment: String,
        command: crate::execution::Command,
    },
    Catalog {
        command: crate::catalog::governance::ReadCommand,
    },
    Projects {},
    Project {
        deployment: String,
    },
    Policy {
        deployment: String,
    },
    SetRole {
        deployment: String,
        subject: Subject,
        role: Option<Role>,
        expected_policy: i64,
        key: String,
    },
    Sources {
        deployment: String,
    },
    Source {
        deployment: String,
        revision: String,
    },
    SaveSource {
        deployment: String,
        asset: String,
        kind: String,
        contents: String,
        expected_head: Option<String>,
        expected_policy: i64,
        key: String,
    },
    AdmitExecution {
        deployment: String,
        source_revision: String,
        effective_principal: Option<String>,
        expected_policy: i64,
        key: String,
    },
    Executions {
        deployment: String,
    },
    Execution {
        deployment: String,
        id: String,
    },
    StopExecution {
        deployment: String,
        id: String,
        expected_policy: i64,
        key: String,
    },
    /// Closed, deliberately denied inventory entries for not-yet-qualified paths.
    Unavailable {
        deployment: String,
        operation: inventory::GatedOperation,
    },
}
impl Command {
    pub(crate) fn deployment(&self) -> Option<&str> {
        match self {
            Self::Projects {} | Self::Catalog { .. } => None,
            Self::Workspace { command } => command.deployment(),
            Self::Data { deployment, .. }
            | Self::Runtime { deployment, .. }
            | Self::Project { deployment }
            | Self::Policy { deployment }
            | Self::SetRole { deployment, .. }
            | Self::Sources { deployment }
            | Self::Source { deployment, .. }
            | Self::SaveSource { deployment, .. }
            | Self::AdmitExecution { deployment, .. }
            | Self::Executions { deployment }
            | Self::Execution { deployment, .. }
            | Self::StopExecution { deployment, .. }
            | Self::Unavailable { deployment, .. } => Some(deployment),
        }
    }
    pub(crate) fn mutation(&self) -> Option<(i64, &str)> {
        match self {
            Self::SetRole {
                expected_policy,
                key,
                ..
            }
            | Self::SaveSource {
                expected_policy,
                key,
                ..
            }
            | Self::AdmitExecution {
                expected_policy,
                key,
                ..
            }
            | Self::StopExecution {
                expected_policy,
                key,
                ..
            } => Some((*expected_policy, key)),
            Self::Workspace { .. }
            | Self::Projects {}
            | Self::Data { .. }
            | Self::Catalog { .. }
            | Self::Runtime { .. }
            | Self::Project { .. }
            | Self::Policy { .. }
            | Self::Sources { .. }
            | Self::Source { .. }
            | Self::Executions { .. }
            | Self::Execution { .. }
            | Self::Unavailable { .. } => None,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum AdminCommand {
    SetDataGrant {
        deployment: String,
        branch: String,
        subject: Subject,
        capability: crate::governed::Capability,
        present: bool,
        expected_policy: i64,
        key: String,
    },
    SetRole {
        deployment: String,
        subject: Subject,
        role: Option<Role>,
        expected_policy: i64,
        key: String,
    },
    SetGrant {
        deployment: String,
        subject: Subject,
        grant: Grant,
        effective_principal: Option<String>,
        source_revision: Option<String>,
        present: bool,
        expected_policy: i64,
        key: String,
    },
    Policy {
        deployment: String,
    },
    Audit {
        deployment: String,
        after: i64,
    },
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope {
    pub api_version: u32,
    pub token: String,
    pub channel: Channel,
    pub csrf: Option<String>,
    pub command: Command,
}
impl std::fmt::Debug for Envelope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AuthorizationEnvelope([redacted])")
    }
}
pub(crate) fn denied() -> crate::store::Error {
    invalid("project action is not authorized")
}
pub(crate) fn key(value: &str) -> Result<()> {
    if value.is_empty() || value.len() > 128 || value.chars().any(char::is_control) {
        return Err(denied());
    }
    Ok(())
}
