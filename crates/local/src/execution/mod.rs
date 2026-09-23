//! Private, opt-in Linux execution. Never delegates to local-owner host workers.
pub(crate) mod files;
pub(crate) mod runtime;
use crate::store::{Result, error::invalid};
pub(crate) use files::{Input, Prepared};
pub(crate) use runtime::Manager;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Dataset {
    pub publication: String,
    pub publication_revision: i64,
    pub table: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Start { id: String, datasets: Vec<Dataset> },
    Poll { id: String },
}
impl Command {
    pub(crate) fn id(&self) -> &str {
        match self {
            Self::Start { id, .. } | Self::Poll { id } => id,
        }
    }
}
pub(crate) fn denied() -> crate::store::Error {
    invalid("isolated execution is unavailable or no longer authorized")
}
pub(crate) fn uuid(value: &str) -> Result<()> {
    value
        .parse::<uuid::Uuid>()
        .map(|_| ())
        .map_err(|_| denied())
}
pub(crate) const LEASE_MS: i64 = 30_000;
pub(crate) const MAX_BYTES: u64 = 256 * 1024 * 1024;
