//! Destination-owned deployment identities. Local OS owner only; no delegated IAM.
use crate::{api::Binding, store::Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use supabricks_core::resource::{DeploymentId, ProjectId};
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Inspect,
    List,
    Create {
        key: String,
        target: Option<String>,
    },
    Attach {
        deployment: DeploymentId,
    },
    Adopt {
        runtime_project: ProjectId,
        key: String,
        target: Option<String>,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Context {
    pub api_version: u32,
    pub definition_id: ProjectId,
    pub deployment_id: DeploymentId,
    pub runtime_project_id: ProjectId,
    pub workspace_id: String,
    pub realm_id: String,
    pub target: String,
    pub legacy: bool,
    pub revision: i64,
    pub actor_id: String,
    pub effective_principal_id: String,
    pub identity_provider: String,
}
impl Context {
    pub fn binding(&self, worktree: &Path) -> Binding {
        Binding {
            project_id: self.runtime_project_id,
            worktree: worktree.to_owned(),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub definition_id: ProjectId,
    pub worktree: PathBuf,
}
impl Source {
    pub fn read(path: &Path) -> Result<Self> {
        let worktree = path.canonicalize()?;
        let config = crate::projects::source_identity(&worktree)?;
        Ok(Self {
            definition_id: config.id,
            worktree,
        })
    }
}
