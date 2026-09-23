//! Typed signed-in console commands. No paths, provider secrets or operator envelopes.
use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Sync {
        deployment: String,
        request: crate::sync::GovernedCommand,
    },
    Context {},
    Snapshot {
        deployment: String,
        export: String,
    },
    Directory {},
    CreateProject {
        name: String,
        key: String,
    },
    Branches {
        deployment: String,
    },
    Policy {
        command: crate::authorization::AdminCommand,
    },
    Catalog {
        command: crate::catalog::governance::AdminCommand,
    },
    Namespace {
        deployment: String,
        ensure: bool,
    },
    Publication {
        deployment: String,
        command: crate::catalog::publication::Command,
    },
    Group {
        label: String,
    },
    Service {
        label: String,
    },
    Membership {
        group: String,
        principal: String,
        present: bool,
    },
    Disable {
        principal: String,
        disabled: bool,
    },
    Revoke {
        principal: String,
    },
    Audit {
        after: i64,
    },
}
impl Command {
    pub(crate) fn deployment(&self) -> Option<&str> {
        match self {
            Self::Snapshot { deployment, .. }
            | Self::Sync { deployment, .. }
            | Self::Branches { deployment }
            | Self::Namespace { deployment, .. }
            | Self::Publication { deployment, .. } => Some(deployment),
            Self::Policy { command } => Some(match command {
                crate::authorization::AdminCommand::SetDataGrant { deployment, .. }
                | crate::authorization::AdminCommand::SetRole { deployment, .. }
                | crate::authorization::AdminCommand::SetGrant { deployment, .. }
                | crate::authorization::AdminCommand::Policy { deployment }
                | crate::authorization::AdminCommand::Audit { deployment, .. } => deployment,
            }),
            _ => None,
        }
    }
}
