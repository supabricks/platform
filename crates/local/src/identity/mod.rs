//! UC09.1 authentication foundation. No governed product ingress is enabled.
//! The control socket is operator-only; remote adapters expose only AuthCommand.
pub mod oidc;
pub mod transport;
use crate::store::{Result, error::invalid};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const VERSION: u32 = 1;
pub const SELF_SCOPE: &str = "identity:self";
pub(crate) fn hash(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}
pub(crate) fn secret() -> Result<String> {
    crate::console::secret()
}
pub(crate) fn id() -> String {
    uuid::Uuid::new_v4().to_string()
}
pub(crate) fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
pub(crate) fn denied() -> crate::store::Error {
    invalid("identity authentication refused")
}
pub(crate) fn label(value: &str) -> Result<()> {
    if value.is_empty() || value.len() > 256 || value.chars().any(char::is_control) {
        return Err(invalid("identity label must contain 1–256 printable bytes"));
    }
    Ok(())
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Channel {
    Browser,
    Cli,
    Service,
}
impl Channel {
    pub(crate) fn name(&self) -> &'static str {
        match self {
            Self::Browser => "browser",
            Self::Cli => "cli",
            Self::Service => "service",
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Context {
    pub api_version: u32,
    pub realm_id: String,
    pub actor_id: String,
    pub effective_principal_id: String,
    pub channel: Channel,
    pub scopes: Vec<String>,
    pub expires_ms: i64,
}
/// Secrets intentionally have redacted Debug. Serialization is only for the
/// protected control socket; adapters must never return this command to clients.
#[derive(Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum AuthCommand {
    Begin {
        provider: String,
        redirect: String,
        binding: String,
        channel: Channel,
    },
    Complete {
        state: String,
        code: String,
        binding: String,
        redirect: String,
        channel: Channel,
    },
    Authenticate {
        token: String,
        channel: Channel,
        csrf: Option<String>,
    },
    Logout {
        token: String,
        channel: Channel,
        csrf: Option<String>,
    },
}
impl std::fmt::Debug for AuthCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AuthCommand([redacted])")
    }
}
#[derive(Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum AdminCommand {
    Status,
    Configure {
        provider: String,
        config: oidc::Config,
    },
    Bootstrap {
        issuer: String,
        subject: String,
        label: String,
    },
    Disable {
        principal: String,
        disabled: bool,
    },
    Group {
        label: String,
    },
    Membership {
        group: String,
        principal: String,
        present: bool,
    },
    Service {
        label: String,
    },
    IssueService {
        principal: String,
        scopes: Vec<String>,
        ttl_seconds: u32,
    },
    Revoke {
        principal: String,
    },
    RotateSessions,
    Audit {
        after: i64,
    },
}
impl std::fmt::Debug for AdminCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AdminCommand([redacted])")
    }
}
