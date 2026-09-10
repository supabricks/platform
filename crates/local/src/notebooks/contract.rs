//! Notebook handles are ephemeral and fenced by both daemon and kernel generation.
use crate::{
    console::workspace::Target,
    store::{Result, error::invalid},
};
use serde::{Deserialize, Serialize};
use supabricks_core::resource::OperationId;

pub const PROTOCOL: u32 = 1;
pub const FRAME_BYTES: usize = 2 * 1024 * 1024;
pub const CELL_OUTPUT_BYTES: usize = 1024 * 1024;
pub const SERVER_RSS_BYTES: u64 = 512 * 1024 * 1024;
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub lifetime_ms: u64,
    pub idle_ms: u64,
    pub kernel_rss_bytes: u64,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            lifetime_ms: 900_000,
            idle_ms: 300_000,
            kernel_rss_bytes: 1024 * 1024 * 1024,
        }
    }
}
impl Limits {
    pub fn validate(&self) -> Result<()> {
        if !(10_000..=900_000).contains(&self.lifetime_ms)
            || !(10_000..=300_000).contains(&self.idle_ms)
            || !(64 * 1024 * 1024..=1024 * 1024 * 1024).contains(&self.kernel_rss_bytes)
        {
            return Err(invalid(
                "notebook limits require lifetime 10–900s, idle 10–300s and kernel RSS 64–1024 MiB",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Create {
        key: String,
        target: Target,
        #[serde(default)]
        epoch: Option<supabricks_core::resource::EpochId>,
        #[serde(default)]
        limits: Limits,
    },
    List,
    Status {
        id: OperationId,
        generation: u64,
    },
    Start {
        id: OperationId,
        generation: u64,
        key: String,
    },
    Interrupt {
        id: OperationId,
        generation: u64,
        key: String,
    },
    Restart {
        id: OperationId,
        generation: u64,
        key: String,
    },
    Shutdown {
        id: OperationId,
        generation: u64,
        key: String,
    },
}
/// Only the authenticated console bridge sends these. Never deserialize them as
/// browser workspace commands or expose credentials in the public status response.
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub enum Transport {
    Connect { id: OperationId, generation: u64 },
    Check { id: OperationId, generation: u64 },
    Revoke,
}
pub fn key(value: &str) -> Result<()> {
    if value.is_empty() || value.len() > 128 {
        return Err(invalid("notebook request key requires 1–128 bytes"));
    }
    Ok(())
}
