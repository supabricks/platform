//! The analytics domain: the versions of a branch's analytical copy, the
//! sessions that read one and the queries run in a session. A version is what
//! the platform calls an epoch or a snapshot, and a pin is a snapshot lease.
use super::{
    Call, Local, Problem, Reply, databases, projects,
    shared::{cursor, fields, idempotent, internal_id, page, public_id, timestamp},
};
use crate::api::Action;
use crate::store::{AnalyticalSession, Error, ProjectRecord, Snapshot, SnapshotLease, Store};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use supabricks_core::error::OperationError;
use supabricks_core::resource::{BranchId, EpochId, LeaseId, OperationId, ProjectId};

/// How many sessions an installation runs at once.
const SLOTS: usize = 2;

/// A project and the ID its branches know it by.
struct Place {
    project: ProjectRecord,
    runtime: ProjectId,
}
fn place(store: &Store, project: &str) -> Result<Place, Problem> {
    let project = projects::found(store, project)?;
    Ok(Place {
        runtime: store
            .project_runtime(&project.id)
            .map_err(|e| Problem::from(&e))?,
        project,
    })
}
fn conflict(detail: &str) -> Problem {
    Problem::new(409, "conflict", detail)
}
fn no_branch() -> Problem {
    Problem::not_found("There is no such branch in this project.")
}
fn no_version() -> Problem {
    Problem::not_found("There is no such analytical version.")
}
fn no_session() -> Problem {
    Problem::not_found("There is no such analytical session.")
}
fn no_pin() -> Problem {
    Problem::not_found("There is no such pin, or it has expired.")
}
fn no_refresh() -> Problem {
    Problem::not_found("There is no such refresh.")
}
fn parsed<T: std::str::FromStr>(prefix: &str, public: &str) -> Option<T> {
    internal_id(prefix, public).and_then(|id| id.parse().ok())
}
/// The platform's reason for refusing, as a sentence for a person.
fn refused(error: &Error) -> Problem {
    let detail = match error {
        Error::Operation(OperationError::Conflict(detail)) => detail.as_str(),
        Error::Operation(OperationError::InvalidInput(e)) => e.message.as_str(),
        _ => return Problem::from(error),
    };
    match detail {
        d if d.starts_with("both analytical session slots are occupied") => Problem::new(
            409,
            "capacity_exhausted",
            "Both analytical session slots are in use. Close a session first.",
        ),
        "installation snapshot lease limit reached" => Problem::new(
            409,
            "capacity_exhausted",
            "This installation holds as many pins as it keeps.",
        ),
        "analytical session is not ready or has expired" => {
            conflict("The session is not ready, or has ended.")
        }
        "session already has a running SQL request" => {
            conflict("The session is already running a query.")
        }
        "snapshot is not available for new readers" | "snapshot is unavailable" => {
            conflict("The version is not available.")
        }
        "selected epoch is unavailable or belongs to another branch" => {
            conflict("The version is not available.")
        }
        "one export at a time is allowed per installation" => conflict(
            "Another copy of a branch's tables is being made or cleared away. Try again in a moment.",
        ),
        "engine is disabled" | "session.py must be installed beside the configured export.py" => {
            no_runtime()
        }
        _ => Problem::from(error),
    }
}
fn no_runtime() -> Problem {
    Problem::unavailable("Analytics needs the database engines and the analytical runtime.")
}
/// What a refresh and a session need before they are asked for.
fn runtime(store: &Store, host: &Local) -> Result<(), Problem> {
    if host.cell.is_none() || crate::sessions::worker_config(store).is_err() {
        return Err(no_runtime());
    }
    Ok(())
}
/// A branch of the project that is a branch: the record that holds a backup
/// has no analytical copy.
fn branch_of(store: &Store, at: &Place, branch: &str) -> Result<BranchId, Problem> {
    let id: BranchId = parsed("br", branch).ok_or_else(no_branch)?;
    match store.database_of(at.runtime, id) {
        Ok(Some(_)) => Ok(id),
        _ => Err(no_branch()),
    }
}
fn database(store: &Store, at: &Place, branch: BranchId) -> Value {
    store
        .database_of(at.runtime, branch)
        .ok()
        .flatten()
        .map_or(Value::Null, |id| json!(public_id("db", &id.to_string())))
}
fn whole(body: &Map<String, Value>, name: &str, default: Option<u64>) -> Result<u64, Problem> {
    match body.get(name) {
        None => default.ok_or_else(|| Problem::invalid(format!("{name} is required."))),
        Some(value) => value
            .as_u64()
            .ok_or_else(|| Problem::invalid(format!("{name} must be a whole number."))),
    }
}
fn located(mut reply: Reply, at: &Place, rest: String) -> Reply {
    reply.status = 201;
    reply.headers.insert(
        "Location".into(),
        format!(
            "{}/projects/{}/analytics/{rest}",
            super::PREFIX,
            public_id("prj", &at.project.id)
        ),
    );
    reply
}

// ---- Versions ----

fn version_id(id: impl ToString) -> String {
    public_id("ver", &id.to_string())
}
/// A version without what only its detail carries. `snapshot` must have been
/// read with its descriptor.
fn version_of(store: &Store, at: &Place, snapshot: &Snapshot) -> Result<Value, Problem> {
    let publication = &snapshot.publication;
    let manifest = publication
        .descriptor
        .as_ref()
        .map_or(&Value::Null, |d| &d["manifest"]);
    let tables = manifest["tables"].as_array();
    let pipeline = store
        .sync_export_policy(publication.export_id)
        .map_err(|e| Problem::from(&e))?;
    Ok(json!({
        "id": version_id(publication.epoch_id),
        "project_id": public_id("prj", &at.project.id),
        "database_id": database(store, at, publication.branch_id),
        "branch_id": public_id("br", &publication.branch_id.to_string()),
        "sequence": publication.ordinal,
        "state": snapshot.state,
        "current": snapshot.current,
        "error": snapshot.error,
        "requested_at": timestamp(publication.requested_at_ms),
        "published_at": publication.published_at_ms.map(timestamp),
        "source_lsn": store.snapshot_source_lsn(publication.epoch_id).map_err(|e| Problem::from(&e))?,
        "pipeline_id": pipeline.map(|p| public_id("sync", &p.id.to_string())),
        "table_count": tables.map(Vec::len),
        "row_count": tables.and_then(|tables| tables.iter().map(|t| t["rows"].as_u64()).sum::<Option<u64>>()),
        "size_bytes": manifest["files"].as_array().and_then(|files| files.iter().map(|f| f["bytes"].as_u64()).sum::<Option<u64>>()),
    }))
}
fn found_version(
    store: &Store,
    project: &str,
    version: &str,
) -> Result<(Place, EpochId, Snapshot), Problem> {
    let at = place(store, project)?;
    let id: EpochId = parsed("ver", version).ok_or_else(no_version)?;
    let snapshot = store
        .snapshot_record(at.runtime, id, true)
        .map_err(|_| no_version())?;
    if snapshot.state == "deleted" {
        return Err(no_version());
    }
    Ok((at, id, snapshot))
}
fn pin_of(pin: &SnapshotLease) -> Value {
    json!({
        "id": public_id("pin", &pin.id.to_string()),
        "version_id": version_id(pin.epoch_id),
        "expires_at": timestamp(pin.expires_at_ms),
    })
}

pub fn versions(
    store: &mut Store,
    call: &Call,
    project: &str,
    branch: &str,
) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let branch = branch_of(store, &at, branch)?;
    let limit = call.limit()?;
    let wrong = || Problem::invalid("cursor is not one this server issued.");
    let before = match call.cursor()? {
        None => None,
        Some(position) => {
            let [ordinal] = <[String; 1]>::try_from(position).map_err(|_| wrong())?;
            Some(ordinal.parse::<i64>().map_err(|_| wrong())?)
        }
    };
    let mut rows = store
        .kept_snapshots(branch, before, limit + 1)
        .map_err(|e| Problem::from(&e))?;
    let next = (rows.len() > limit).then(|| {
        rows.truncate(limit);
        cursor(&[&rows.last().unwrap().0.to_string()])
    });
    let mut items = Vec::new();
    for (_, id) in rows {
        let snapshot = store
            .snapshot_record(at.runtime, id, true)
            .map_err(|e| Problem::from(&e))?;
        items.push(version_of(store, &at, &snapshot)?);
    }
    Ok(page(items, next))
}

pub fn version(store: &mut Store, project: &str, version: &str) -> Result<Reply, Problem> {
    let (at, id, snapshot) = found_version(store, project, version)?;
    let mut body = version_of(store, &at, &snapshot)?;
    let manifest = snapshot
        .publication
        .descriptor
        .as_ref()
        .map_or(&Value::Null, |d| &d["manifest"]);
    let list = |value: &Value| value.as_array().cloned().unwrap_or_default();
    body["observed_at"] = json!(manifest["observed_at_ms"].as_i64().map(timestamp));
    body["tables"] = list(&manifest["tables"])
        .iter()
        .map(|table| {
            json!({
                "schema": table["schema"].as_str().unwrap_or_default(),
                "name": table["name"].as_str().unwrap_or_default(),
                "rows": table["rows"].as_u64(),
                "columns": list(&table["columns"]).iter().map(|column| json!({
                    "name": column["name"].as_str().unwrap_or_default(),
                    "type": column["arrow_type"].as_str(),
                    "nullable": column["nullable"].as_bool(),
                })).collect::<Vec<_>>(),
            })
        })
        .collect();
    body["omitted_tables"] = list(&manifest["omitted_relations"])
        .iter()
        .map(|relation| {
            json!({
                "schema": relation["schema"].as_str().unwrap_or_default(),
                "name": relation["name"].as_str().unwrap_or_default(),
                "reason": relation["reason"].as_str().unwrap_or_default(),
            })
        })
        .collect();
    let sessions = store.snapshot_sessions(id).map_err(|e| Problem::from(&e))?;
    let pins = store.snapshot_pins(id).map_err(|e| Problem::from(&e))?;
    body["readers"] = json!({
        "session_ids": sessions.iter().map(|id| public_id("ses", &id.to_string())).collect::<Vec<_>>(),
        "pins": pins.iter().map(pin_of).collect::<Vec<_>>(),
    });
    Ok(Reply::ok(body))
}

pub fn collect(
    store: &mut Store,
    call: &Call,
    project: &str,
    branch: &str,
) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let id = branch_of(store, &at, branch)?;
    let keep = whole(fields(call, &["keep"])?, "keep", None)?;
    if !(1..=100).contains(&keep) {
        return Err(Problem::invalid("keep must be from 1 to 100."));
    }
    let scope = format!("{}:collectAnalyticalVersions:{branch}", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let done = store
            .collect_snapshots(at.runtime, id, keep as usize)
            .map_err(|e| refused(&e))?;
        let deleting: Vec<String> = done["deleting"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(version_id)
            .collect();
        Ok(Reply::ok(json!({
            "branch_id": public_id("br", &id.to_string()),
            "keep": keep,
            "deleting": deleting,
        })))
    })
}

pub fn pin(store: &mut Store, call: &Call, project: &str, version: &str) -> Result<Reply, Problem> {
    let (at, id, _) = found_version(store, project, version)?;
    let ttl = lifetime(call)?;
    let scope = format!("{}:pinAnalyticalVersion:{version}", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let pin = store
            .pin_snapshot(at.runtime, id, ttl)
            .map_err(|e| refused(&e))?;
        Ok(located(
            Reply::ok(pin_of(&pin)),
            &at,
            format!("pins/{}", public_id("pin", &pin.id.to_string())),
        ))
    })
}
/// `ttl_seconds` of a pin, in milliseconds.
fn lifetime(call: &Call) -> Result<u64, Problem> {
    let seconds = whole(fields(call, &["ttl_seconds"])?, "ttl_seconds", None)?;
    if !(1..=86_400).contains(&seconds) {
        return Err(Problem::invalid("ttl_seconds must be from 1 to 86400."));
    }
    Ok(seconds * 1000)
}
fn found_pin(store: &Store, project: &str, pin: &str) -> Result<(Place, SnapshotLease), Problem> {
    let at = place(store, project)?;
    let id: LeaseId = parsed("pin", pin).ok_or_else(no_pin)?;
    let pin = store.snapshot_pin(at.runtime, id).map_err(|_| no_pin())?;
    Ok((at, pin))
}
pub fn renew_pin(
    store: &mut Store,
    call: &Call,
    project: &str,
    pin: &str,
) -> Result<Reply, Problem> {
    let (at, found) = found_pin(store, project, pin)?;
    let ttl = lifetime(call)?;
    let scope = format!("{}:renewAnalyticalPin:{pin}", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let pin = store
            .renew_snapshot_lease(at.runtime, found.id, ttl)
            .map_err(|e| refused(&e))?;
        Ok(Reply::ok(pin_of(&pin)))
    })
}
pub fn release_pin(
    store: &mut Store,
    call: &Call,
    project: &str,
    pin: &str,
) -> Result<Reply, Problem> {
    let scope = format!("{}:releaseAnalyticalPin:{pin}", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let (at, found) = found_pin(store, project, pin)?;
        store
            .release_snapshot_lease(at.runtime, found.id)
            .map_err(|e| refused(&e))?;
        Ok(Reply {
            status: 204,
            headers: BTreeMap::new(),
            body: None,
            wait: None,
        })
    })
}

// ---- Refreshes ----

/// The API's view of a refresh, from the platform's status of one.
fn refresh_of(status: &Value) -> Value {
    let publication = &status["publication"];
    let state = match status["state"].as_str() {
        Some("published") => "succeeded",
        Some(ended @ ("failed" | "cancelled")) => ended,
        _ => "running",
    };
    json!({
        "id": public_id("rfr", status["id"].as_str().unwrap_or_default()),
        "branch_id": public_id("br", status["export"]["source_id"].as_str().unwrap_or_default()),
        "state": state,
        "error": match state {
            "failed" | "cancelled" => status["error"].as_str().or(publication["error"].as_str()).or(Some(state)),
            _ => None,
        },
        "version_id": (state == "succeeded").then(|| version_id(publication["epoch_id"].as_str().unwrap_or_default())),
    })
}
fn found_refresh(
    store: &Store,
    project: &str,
    refresh: &str,
) -> Result<(Place, OperationId, Value), Problem> {
    let at = place(store, project)?;
    let id: OperationId = parsed("rfr", refresh).ok_or_else(no_refresh)?;
    let status = store
        .refresh_status(at.runtime, id)
        .map_err(|_| no_refresh())?;
    Ok((at, id, status))
}
pub fn start_refresh(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    branch: &str,
) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let id = branch_of(store, &at, branch)?;
    let mut limits = crate::store::ExportLimits::default();
    if let Some(body) = call.body.as_ref() {
        let body = body
            .as_object()
            .filter(|body| body.keys().all(|key| key == "limits"))
            .ok_or_else(|| Problem::invalid("The body is an object with limits, or nothing."))?;
        if let Some(given) = body.get("limits") {
            let given = given
                .as_object()
                .filter(|o| o.keys().all(|k| k == "max_bytes" || k == "timeout_ms"))
                .ok_or_else(|| {
                    Problem::invalid("limits must be an object with max_bytes or timeout_ms.")
                })?;
            limits.max_bytes = whole(given, "max_bytes", Some(limits.max_bytes))?;
            limits.timeout_ms = whole(given, "timeout_ms", Some(limits.timeout_ms))?;
            limits.validate().map_err(|e| Problem::from(&e))?;
        }
    }
    let host = databases::owner(host)?;
    let scope = format!("{}:startAnalyticalRefresh:{branch}", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        runtime(store, host)?;
        let action = Action::AnalyticsRefresh {
            branch: id.to_string(),
            key: format!("api:startAnalyticalRefresh:{key}"),
            limits,
        };
        let status = databases::act(store, host, &at.project, action).map_err(|e| refused(&e))?;
        let body = refresh_of(&status);
        let rest = format!("refreshes/{}", body["id"].as_str().unwrap_or_default());
        Ok(located(Reply::ok(body), &at, rest))
    })
}
pub fn refresh(store: &mut Store, project: &str, refresh: &str) -> Result<Reply, Problem> {
    Ok(Reply::ok(refresh_of(
        &found_refresh(store, project, refresh)?.2,
    )))
}
pub fn cancel_refresh(
    store: &mut Store,
    call: &Call,
    project: &str,
    refresh: &str,
) -> Result<Reply, Problem> {
    let (at, id, status) = found_refresh(store, project, refresh)?;
    let scope = format!("{}:cancelAnalyticalRefresh:{refresh}", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let seen = refresh_of(&status);
        match seen["state"].as_str() {
            Some("succeeded") => {
                return Err(conflict(
                    "The refresh succeeded and its version is published.",
                ));
            }
            Some("failed" | "cancelled") => return Ok(Reply::ok(seen)),
            _ => {}
        }
        let status = crate::sessions::Sessions::cancel_refresh(store, at.runtime, id)
            .map_err(|e| refused(&e))?;
        Ok(Reply::ok(refresh_of(&status)))
    })
}

// ---- Sessions ----

fn session_of(store: &Store, at: &Place, session: &AnalyticalSession, notebook: bool) -> Value {
    let live = !matches!(session.state.as_str(), "closing" | "closed" | "failed");
    json!({
        "id": public_id("ses", &session.id.to_string()),
        "project_id": public_id("prj", &at.project.id),
        "database_id": database(store, at, session.branch_id),
        "branch_id": public_id("br", &session.branch_id.to_string()),
        "version_id": session.epoch_id.map(version_id),
        "refresh_id": session.refresh_id.map(|id| public_id("rfr", &id.to_string())),
        "purpose": if notebook { "notebook" } else { "query" },
        "state": session.state,
        "error": if live { None } else { session.error.as_deref() },
        "created_at": timestamp(session.created_at_ms),
        "expires_at": timestamp(session.expires_at_ms),
        "spark_connect_url": if session.state == "ready" { session.endpoint.as_deref() } else { None },
    })
}
fn found_session(
    store: &Store,
    project: &str,
    session: &str,
) -> Result<(Place, AnalyticalSession), Problem> {
    let at = place(store, project)?;
    let id: OperationId = parsed("ses", session).ok_or_else(no_session)?;
    let session = store
        .analytical_session(at.runtime, id)
        .map_err(|_| no_session())?;
    Ok((at, session))
}
fn one_session(store: &Store, at: &Place, session: &AnalyticalSession) -> Result<Reply, Problem> {
    let notebook = store
        .notebook_session(session.id)
        .map_err(|e| Problem::from(&e))?;
    Ok(Reply::ok(session_of(store, at, session, notebook)))
}

pub fn sessions(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let limit = call.limit()?;
    let active = match call.parameter("active")? {
        None | Some("false") => false,
        Some("true") => true,
        Some(_) => return Err(Problem::invalid("active must be true or false.")),
    };
    let wrong = || Problem::invalid("cursor is not one this server issued.");
    let before = match call.cursor()? {
        None => None,
        Some(position) => {
            let [made, id] = <[String; 2]>::try_from(position).map_err(|_| wrong())?;
            Some((made.parse::<i64>().map_err(|_| wrong())?, id))
        }
    };
    let mut rows = store
        .analytical_sessions_page(at.runtime, active, before, limit + 1)
        .map_err(|e| Problem::from(&e))?;
    let next = (rows.len() > limit).then(|| {
        rows.truncate(limit);
        let last = &rows.last().unwrap().0;
        cursor(&[&last.created_at_ms.to_string(), &last.id.to_string()])
    });
    Ok(page(
        rows.iter()
            .map(|(session, notebook)| session_of(store, &at, session, *notebook))
            .collect(),
        next,
    ))
}

pub fn capacity(store: &mut Store, project: &str) -> Result<Reply, Problem> {
    place(store, project)?;
    let open = store
        .active_analytical_sessions()
        .map_err(|e| Problem::from(&e))?
        .len();
    Ok(Reply::ok(
        json!({"session_slots": SLOTS, "sessions_open": open}),
    ))
}

pub fn open(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let body = fields(call, &["branch_id", "version_id", "ttl_seconds"])?;
    let text = |name: &str| match body.get(name) {
        None => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.as_str())),
        Some(_) => Err(Problem::invalid(format!("{name} must be text."))),
    };
    let (branch, epoch) = match (text("branch_id")?, text("version_id")?) {
        (Some(branch), None) => (Some(branch_of(store, &at, branch)?), None),
        (None, Some(version)) => {
            let (_, id, snapshot) =
                found_version(store, &public_id("prj", &at.project.id), version)?;
            if snapshot.state != "available" {
                return Err(conflict("The version is not available."));
            }
            (None, Some(id))
        }
        _ => {
            return Err(Problem::invalid(
                "Give branch_id or version_id, and not both.",
            ));
        }
    };
    let ttl = whole(body, "ttl_seconds", Some(900))?;
    if !(10..=3600).contains(&ttl) {
        return Err(Problem::invalid("ttl_seconds must be from 10 to 3600."));
    }
    let host = databases::owner(host)?;
    let scope = format!("{}:openAnalyticalSession", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        runtime(store, host)?;
        let action = Action::AnalyticsOpen {
            catalog: false,
            branch: branch.map(|id| id.to_string()),
            epoch,
            key: format!("api:openAnalyticalSession:{key}"),
            ttl_ms: ttl * 1000,
        };
        let made = databases::act(store, host, &at.project, action).map_err(|e| refused(&e))?;
        let session: AnalyticalSession = serde_json::from_value(made)
            .map_err(|_| Problem::new(500, "internal_error", "The session is unreadable."))?;
        let rest = format!("sessions/{}", public_id("ses", &session.id.to_string()));
        Ok(located(one_session(store, &at, &session)?, &at, rest))
    })
}

pub fn session(store: &mut Store, project: &str, session: &str) -> Result<Reply, Problem> {
    let (at, session) = found_session(store, project, session)?;
    one_session(store, &at, &session)
}

pub fn close(
    store: &mut Store,
    call: &Call,
    project: &str,
    session: &str,
) -> Result<Reply, Problem> {
    let (at, found) = found_session(store, project, session)?;
    let scope = format!("{}:closeAnalyticalSession:{session}", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let closed = store
            .close_analytical_session(at.runtime, found.id, "closed")
            .map_err(|e| refused(&e))?;
        one_session(store, &at, &closed)
    })
}

// ---- Queries ----

fn query_of(query: &Value) -> Value {
    let complete = query["state"] == "complete";
    json!({
        "id": public_id("aq", query["id"].as_str().unwrap_or_default()),
        "session_id": public_id("ses", query["session_id"].as_str().unwrap_or_default()),
        "version_id": query["epoch_id"].as_str().map(version_id),
        "state": query["state"],
        "sql": query["sql"],
        "columns": if complete { query["columns"].clone() } else { Value::Null },
        "rows": if complete { text_rows(&query["rows"]) } else { Value::Null },
        "truncated": query["truncated"].as_bool().unwrap_or(false),
        "error": query["error"],
    })
}
/// Rows with every value as text or null, whatever the worker wrote.
fn text_rows(rows: &Value) -> Value {
    let cell = |value: &Value| match value {
        Value::Null => Value::Null,
        Value::String(text) => json!(text),
        other => json!(other.to_string()),
    };
    rows.as_array()
        .map(|rows| {
            rows.iter()
                .map(|row| {
                    row.as_array()
                        .map(|row| row.iter().map(cell).collect::<Vec<_>>())
                })
                .collect()
        })
        .unwrap_or(Value::Null)
}

pub fn run_query(
    store: &mut Store,
    call: &Call,
    project: &str,
    session: &str,
) -> Result<Reply, Problem> {
    let (at, found) = found_session(store, project, session)?;
    let body = fields(call, &["sql", "max_rows", "max_bytes", "timeout_ms"])?;
    let sql = match body.get("sql") {
        Some(Value::String(sql)) if !sql.is_empty() && sql.len() <= 32768 => sql.clone(),
        Some(Value::String(_)) => {
            return Err(Problem::invalid("sql must be from 1 byte to 32 KiB."));
        }
        _ => return Err(Problem::invalid("sql is required, as text.")),
    };
    let within = |name: &str, default: u64, least: u64, most: u64| {
        let value = whole(body, name, Some(default))?;
        if !(least..=most).contains(&value) {
            return Err(Problem::invalid(format!(
                "{name} must be from {least} to {most}."
            )));
        }
        Ok(value)
    };
    let max_rows = within("max_rows", 200, 1, 1000)? as usize;
    let max_bytes = within("max_bytes", 262_144, 1024, 262_144)? as usize;
    let timeout_ms = within("timeout_ms", 10_000, 100, 30_000)?;
    let scope = format!("{}:runAnalyticalQuery:{session}", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let query = crate::sessions::Sessions::query(
            store,
            at.runtime,
            found.id,
            sql.clone(),
            max_rows,
            max_bytes,
            timeout_ms,
        )
        .map_err(|e| refused(&e))?;
        let mut reply = Reply::ok(query_of(&query));
        reply.status = 201;
        Ok(reply)
    })
}

pub fn query(store: &mut Store, project: &str, session: &str) -> Result<Reply, Problem> {
    let (_, found) = found_session(store, project, session)?;
    match &found.query {
        Some(query) => Ok(Reply::ok(query_of(query))),
        None => Err(Problem::not_found("The session has run no query.")),
    }
}
