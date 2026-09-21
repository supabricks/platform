//! New control routes must explicitly choose a boundary; no default owner route.
use crate::daemon::Request;
use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GatedOperation {
    CatalogRead,
    CatalogGrant,
    PostgresRead,
    PostgresWrite,
    Connect,
    Deploy,
    Import,
    Export,
    Backup,
    Restore,
    Delete,
    WorkloadLaunch,
    Logs,
    ArtifactDownload,
    WebSocket,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Boundary {
    Operator,
    Identity,
    Project,
}
/// Exhaustive on the wire enum. All legacy control/console/stream requests are
/// operator-only; authenticated actors can use only the typed project commands.
pub fn boundary(request: &Request) -> Boundary {
    match request {
        Request::Authorized { .. } => Boundary::Project,
        Request::IdentityAuth { .. } => Boundary::Identity,
        Request::CatalogGovernance { .. }
        | Request::AuthorizationAdmin { .. }
        | Request::IdentityAdmin { .. }
        | Request::CatalogService { .. }
        | Request::ResolveBinding { .. }
        | Request::Project { .. }
        | Request::NotebookTransport { .. }
        | Request::NotebookHeartbeat { .. }
        | Request::ConsoleAction { .. }
        | Request::ConsoleOpen { .. }
        | Request::ConsoleOverview { .. }
        | Request::Api { .. }
        | Request::Status
        | Request::RegisterProject { .. }
        | Request::Submit { .. }
        | Request::Operation { .. }
        | Request::Pending
        | Request::Branch { .. }
        | Request::RenameBranch { .. }
        | Request::SelectWorktree { .. }
        | Request::Selection { .. }
        | Request::ListBranches { .. }
        | Request::GetBranch { .. }
        | Request::Connection { .. }
        | Request::AcquireLease { .. }
        | Request::ReleaseLease { .. }
        | Request::RenewLease { .. }
        | Request::Shutdown
        | Request::AuthorizeProcess { .. } => Boundary::Operator,
    }
}
