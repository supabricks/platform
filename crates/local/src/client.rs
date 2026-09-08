//! Shared CLI/MCP transport. No SQLite access and no implicit lifecycle retries.
use crate::{
    api::{Action, Binding, VERSION},
    daemon::{Envelope, Request},
    project::ProjectConfig,
    store::{Error, Result, error::invalid},
};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    time::Duration,
};
use supabricks_core::error::OperationError;
pub const RESPONSE_LIMIT: u64 = 2 * 1024 * 1024;
pub fn request(root: &Path, request: Request) -> Result<Value> {
    request_timeout(root, request, Duration::from_secs(50))
}
pub(crate) fn request_timeout(root: &Path, request: Request, timeout: Duration) -> Result<Value> {
    let mut stream = UnixStream::connect(root.join("control.sock")).map_err(|_| {
        OperationError::Unavailable(
            "daemon unavailable; run supabricks up or doctor with the same data directory".into(),
        )
    })?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout.min(Duration::from_secs(5))))?;
    let wire = serde_json::to_vec(&Envelope {
        version: 1,
        request,
    })?;
    if wire.len() + 1 > 65536 {
        return Err(invalid("request exceeds 64 KiB"));
    }
    stream.write_all(&wire)?;
    stream.write_all(b"\n")?;
    let mut bytes = Vec::new();
    BufReader::new(stream)
        .take(RESPONSE_LIMIT + 1)
        .read_until(b'\n', &mut bytes)?;
    if bytes.len() as u64 > RESPONSE_LIMIT || bytes.last() != Some(&b'\n') {
        return Err(OperationError::Unavailable(
            "incomplete or oversized daemon response; inspect operation before retrying a write"
                .into(),
        )
        .into());
    }
    let value: Value = serde_json::from_slice(&bytes)?;
    if value["version"] != 1 {
        return Err(invalid("unsupported daemon response version"));
    }
    if let Some(error) = value.get("error") {
        return Err(serde_json::from_value::<OperationError>(error.clone())
            .map(Error::from)
            .unwrap_or_else(|_| {
                OperationError::Unavailable("daemon request failed; inspect doctor".into()).into()
            }));
    }
    value
        .get("result")
        .cloned()
        .ok_or_else(|| invalid("daemon response missing result"))
}
#[derive(Clone)]
pub struct Client {
    pub root: PathBuf,
    pub binding: Binding,
}
impl Client {
    pub fn bind(root: &Path, worktree: &Path) -> Result<Self> {
        let worktree = worktree.canonicalize()?;
        let config = ProjectConfig::read(&worktree)?;
        Ok(Self {
            root: root.to_owned(),
            binding: Binding {
                project_id: config.id,
                worktree,
            },
        })
    }
    pub fn call(&self, action: Action) -> Result<Value> {
        request(
            &self.root,
            Request::Api {
                api_version: VERSION,
                binding: self.binding.clone(),
                action,
            },
        )
    }
}
pub fn project_directory(explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return Ok(path.canonicalize()?);
    }
    let cwd = std::env::current_dir()?;
    cwd.ancestors()
        .find(|p| p.join("supabricks.toml").is_file())
        .map(Path::to_owned)
        .ok_or_else(|| {
            invalid("no supabricks.toml found; run supabricks init or supply --project PATH")
        })
}
pub fn diagnostic(error: &Error) -> Value {
    let (code, message, hint, retryable, exit) = match error {
        Error::Operation(OperationError::InvalidInput(e)) => (
            "invalid_input",
            e.message.clone(),
            e.hint.as_str(),
            false,
            2,
        ),
        Error::Operation(OperationError::NotFound(s)) => (
            "not_found",
            s.clone(),
            "list branches and verify the project, worktree and resource ID",
            false,
            3,
        ),
        Error::Operation(OperationError::Conflict(s)) => (
            "conflict",
            s.clone(),
            "inspect branch revision, operation progress, children and active connections before retrying",
            false,
            4,
        ),
        Error::Operation(OperationError::Unavailable(s)) => (
            "unavailable",
            s.clone(),
            "run doctor; inspect operation status before retrying a mutation or SQL write",
            true,
            5,
        ),
        Error::Operation(OperationError::Query { sqlstate, message }) => (
            "sql_error",
            format!("{sqlstate}: {message}"),
            "correct the SQL or inspect limits; writes are never automatically retried",
            false,
            6,
        ),
        Error::Io(e) => (
            "io_error",
            e.to_string(),
            "check local paths, permissions and daemon status",
            false,
            1,
        ),
        _ => (
            "invalid_input",
            "invalid local configuration or response".into(),
            "check project configuration and command arguments",
            false,
            2,
        ),
    };
    json!({"code":code,"message":message,"hint":hint,"retryable":retryable,"exit_code":exit})
}
