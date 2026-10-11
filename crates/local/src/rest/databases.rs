//! The databases domain. A database is a root branch and the branches made
//! from it, so every change here is the branch action the command line uses.
use super::{
    Call, Caller, Local, Problem, Reply, operations, projects,
    shared::{cursor, etag, fields, idempotent, internal_id, page, public_id, text, timestamp},
};
use crate::api::{Action, Binding};
use crate::store::{DatabaseRecord, Error, ProjectRecord, Store};
use serde_json::{Value, json};
use supabricks_core::error::OperationError;
use supabricks_core::resource::{BranchId, DesiredState};

fn body_of(record: &DatabaseRecord) -> Value {
    json!({
        "id": public_id("db", &record.id.to_string()),
        "name": record.name,
        "state": record.state,
        "postgres_version": record.pg_major.to_string(),
        "default_branch_id": public_id("br", &record.id.to_string()),
        "branch_count": record.branches,
        "holds_project_default": record.holds_default,
        "connections": {
            "open": record.connections,
            "limit": crate::connections::MAX_BRANCH_CONNECTIONS,
        },
        "created_at": timestamp(record.created_at_ms),
        "revision": record.revision,
    })
}
fn missing() -> Problem {
    Problem::not_found("There is no such database.")
}
fn found(
    store: &Store,
    project: &str,
    database: &str,
) -> Result<(ProjectRecord, DatabaseRecord), Problem> {
    let project = projects::found(store, project)?;
    let runtime = store
        .project_runtime(&project.id)
        .map_err(|e| Problem::from(&e))?;
    // An ID of the wrong shape names nothing, exactly like an unknown one.
    let id: BranchId = internal_id("db", database)
        .and_then(|id| id.parse().ok())
        .ok_or_else(missing)?;
    let record = store.database_record(runtime, id).map_err(|_| missing())?;
    Ok((project, record))
}
fn one(record: &DatabaseRecord) -> Reply {
    let mut reply = Reply::ok(body_of(record));
    reply.headers.insert("ETag".into(), etag(record.revision));
    reply
}
/// The owner's console, which every change needs: a governed server does not
/// offer them, so a call without one never reaches a handler that asks.
fn owner<'a, 'b>(host: &'a mut Option<Local<'b>>) -> Result<&'a mut Local<'b>, Problem> {
    host.as_mut()
        .ok_or_else(|| Problem::new(500, "internal_error", "The request has no local console."))
}
/// Run a branch action in the project, as the command line would.
fn act(
    store: &mut Store,
    host: &Local,
    record: &ProjectRecord,
    action: Action,
) -> Result<Value, Error> {
    let binding = Binding {
        project_id: store.project_runtime(&record.id)?,
        // Every action here names its branch, so no worktree selection is read.
        worktree: host.binding.worktree.clone(),
    };
    crate::api::handle(store, host.cell, &binding, action)
}
/// The platform's reason for refusing, as a sentence for a person.
fn reason(detail: &str) -> String {
    match detail {
        "branch has active work leases" => "It has open connections or running work.".into(),
        "delete child branches before their parent" => "It still has other branches.".into(),
        "wait for the current lifecycle operation before requesting running state" => {
            "An earlier change to it is still in progress.".into()
        }
        d if d.starts_with("default branch is protected") => {
            "It holds the project's default branch.".into()
        }
        d if d.starts_with("branch has an active import") => "An import is running on it.".into(),
        d if d.starts_with("branch belongs to a pending project apply") => {
            "A project apply that includes it is in progress.".into()
        }
        "branch has protected branch operations" => "A branch is being created from it.".into(),
        other => format!("The platform refused: {other}."),
    }
}
fn refused(code: &'static str, error: &Error) -> Problem {
    match error {
        Error::Operation(OperationError::Conflict(detail)) => {
            Problem::new(409, code, reason(detail))
        }
        other => Problem::from(other),
    }
}
/// The reply to a change that started an operation.
fn accepted(
    store: &Store,
    project: &ProjectRecord,
    operation: &str,
    key: &str,
) -> Result<Reply, Problem> {
    let unreadable = || Problem::new(500, "internal_error", "The operation is unreadable.");
    store
        .set_operation_api_key(operation, key)
        .map_err(|e| Problem::from(&e))?;
    let entry = store
        .operation_entry(&project.id, operation)
        .map_err(|e| Problem::from(&e))?;
    let view = operations::view(store, project, &entry).ok_or_else(unreadable)?;
    Ok(operations::accepted(project, view))
}
fn operation_id(made: &Value) -> Result<&str, Problem> {
    made["id"]
        .as_str()
        .ok_or_else(|| Problem::new(500, "internal_error", "The operation is unreadable."))
}
fn name_taken(name: &str) -> Problem {
    Problem::new(
        409,
        "name_taken",
        format!(
            "A database or branch named {name} already exists in this project. Choose another name."
        ),
    )
}
/// Whether a database or branch of the project already has the name.
fn taken(
    store: &Store,
    record: &ProjectRecord,
    name: &str,
    except: Option<BranchId>,
) -> Result<bool, Problem> {
    let runtime = store
        .project_runtime(&record.id)
        .map_err(|e| Problem::from(&e))?;
    Ok(store
        .list_branches(runtime, true)
        .map_err(|e| Problem::from(&e))?
        .iter()
        .any(|b| {
            b.branch.name == name
                && Some(b.branch.id) != except
                // A deleted branch keeps its name until its files are gone.
                && (b.endpoint.desired_state != DesiredState::Deleted
                    || b.revision != b.observed_revision)
        }))
}
fn valid(name: &str) -> Result<String, Problem> {
    supabricks_core::validation::valid_name(&json!({"name": name}), "name")
        .map_err(|e| Problem::invalid(e.message))
}

pub fn list(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let project = projects::found(store, project)?;
    let limit = call.limit()?;
    let after = match call.cursor()? {
        None => None,
        Some(position) => match <[String; 2]>::try_from(position) {
            Ok(position) => Some(position),
            Err(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
        },
    };
    let runtime = store
        .project_runtime(&project.id)
        .map_err(|e| Problem::from(&e))?;
    let mut records: Vec<_> = store
        .database_records(runtime)
        .map_err(|e| Problem::from(&e))?
        .into_iter()
        .filter(|r| {
            after
                .as_ref()
                .is_none_or(|[name, id]| (&r.name, &r.id.to_string()) > (name, id))
        })
        .take(limit + 1)
        .collect();
    let next = (records.len() > limit).then(|| {
        records.truncate(limit);
        let last = records.last().unwrap();
        cursor(&[&last.name, &last.id.to_string()])
    });
    Ok(page(records.iter().map(body_of).collect(), next))
}

pub fn get(store: &mut Store, project: &str, database: &str) -> Result<Reply, Problem> {
    Ok(one(&found(store, project, database)?.1))
}

pub fn create(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
) -> Result<Reply, Problem> {
    let project = projects::found(store, project)?;
    let name = valid(
        text(fields(call, &["name"])?, "name")?
            .ok_or_else(|| Problem::invalid("name is required."))?,
    )?;
    let host = owner(host)?;
    let scope = format!("{}:createDatabase", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:createDatabase:{key}");
        let runtime = store
            .project_runtime(&project.id)
            .map_err(|e| Problem::from(&e))?;
        // Refuse a taken name before anything is written, unless this key
        // already created the database and is being repeated.
        let known = store
            .request_for_key(runtime, &native)
            .map_err(|e| Problem::from(&e))?
            .is_some();
        if !known && taken(store, &project, &name, None)? {
            return Err(name_taken(&name));
        }
        let made = act(
            store,
            host,
            &project,
            Action::CreateDatabase {
                name: name.clone(),
                key: native,
            },
        )
        .map_err(|error| match &error {
            Error::Operation(OperationError::Conflict(detail))
                if detail.contains("limit of 32") =>
            {
                Problem::new(
                    409,
                    "capacity_exhausted",
                    "This installation already runs 32 branches. Delete one that is not needed.",
                )
            }
            _ => Problem::from(&error),
        })?;
        accepted(store, &project, operation_id(&made)?, key)
    })
}

pub fn update(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (project, record) = found(store, project, database)?;
    let name = valid(
        text(fields(call, &["name"])?, "name")?
            .ok_or_else(|| Problem::invalid("name is required."))?,
    )?;
    if call
        .expected_revision(false)?
        .is_some_and(|revision| revision != record.revision)
    {
        return Err(Problem::new(
            412,
            "revision_mismatch",
            "The database has changed; read it again and retry.",
        ));
    }
    if name == record.name {
        return Ok(one(&record));
    }
    if record.state == "deleting" {
        return Err(Problem::new(
            409,
            "conflict",
            "The database is being deleted.",
        ));
    }
    if taken(store, &project, &name, Some(record.id))? {
        return Err(name_taken(&name));
    }
    let host = owner(host)?;
    act(
        store,
        host,
        &project,
        Action::RenameBranch {
            branch: record.id.to_string(),
            name,
        },
    )
    .map_err(|e| Problem::from(&e))?;
    let record = store
        .database_record(record.project, record.id)
        .map_err(|e| Problem::from(&e))?;
    Ok(one(&record))
}

/// Start a lifecycle change with the caller's key, or return the one that key
/// already started: the server can stop after the change and before the reply
/// is stored, and the repeat must land on the same operation.
fn change(
    store: &mut Store,
    host: &Local,
    project: &ProjectRecord,
    record: &DatabaseRecord,
    native: String,
    code: &'static str,
    action: impl FnOnce(String) -> Action,
) -> Result<Value, Problem> {
    if let Some(mutation) = store
        .request_for_key(record.project, &native)
        .map_err(|e| Problem::from(&e))?
    {
        let operation = store
            .submit(record.project, &native, mutation)
            .map_err(|e| Problem::from(&e))?;
        return Ok(json!(operation));
    }
    act(store, host, project, action(native)).map_err(|e| refused(code, &e))
}
fn started(store: &Store, native: &str, record: &DatabaseRecord) -> Result<bool, Problem> {
    Ok(store
        .request_for_key(record.project, native)
        .map_err(|e| Problem::from(&e))?
        .is_some())
}

pub fn delete(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (project, record) = found(store, project, database)?;
    let expected = call.expected_revision(true)?;
    let confirm = call.parameter("confirm")?.map(str::to_owned);
    let host = owner(host)?;
    let scope = format!("{}:deleteDatabase", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:deleteDatabase:{key}");
        if !started(store, &native, &record)? {
            if confirm.as_deref() != Some(record.name.as_str()) {
                return Err(Problem::invalid(
                    "confirm must be the database's name, exactly as it is now.",
                ));
            }
            if expected.is_some_and(|revision| revision != record.revision) {
                return Err(Problem::new(
                    412,
                    "revision_mismatch",
                    "The database has changed; read it again and retry.",
                ));
            }
            let blocked = |detail: &str| Problem::new(409, "delete_blocked", detail);
            if record.state == "deleting" {
                return Err(Problem::new(
                    409,
                    "conflict",
                    "The database is already being deleted.",
                ));
            }
            if record.branches > 1 {
                return Err(blocked("It still has other branches. Delete them first."));
            }
            if record.holds_default {
                return Err(blocked(
                    "It holds the project's default branch. Delete the project to remove it.",
                ));
            }
        }
        let made = change(
            store,
            host,
            &project,
            &record,
            native,
            "delete_blocked",
            |key| Action::DeleteBranch {
                branch: record.id.to_string(),
                expected_revision: record.lifecycle_revision,
                key,
                force: false,
            },
        )?;
        accepted(store, &project, operation_id(&made)?, key)
    })
}

fn set_state(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
    desired: DesiredState,
) -> Result<Reply, Problem> {
    let (project, record) = found(store, project, database)?;
    let host = owner(host)?;
    let name = if desired == DesiredState::Suspended {
        "suspendDatabase"
    } else {
        "resumeDatabase"
    };
    let scope = format!("{}:{name}", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:{name}:{key}");
        if !started(store, &native, &record)? {
            let conflict = |detail: &str| Problem::new(409, "conflict", detail);
            if record.state == "deleting" {
                return Err(conflict("The database is being deleted."));
            }
            if desired == DesiredState::Suspended
                && matches!(record.state, "suspended" | "suspending")
            {
                return Err(conflict("The database is already suspended."));
            }
        }
        let made = change(store, host, &project, &record, native, "conflict", |key| {
            Action::SetState {
                branch: record.id.to_string(),
                expected_revision: record.lifecycle_revision,
                desired,
                key,
            }
        })?;
        accepted(store, &project, operation_id(&made)?, key)
    })
}
pub fn suspend(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    set_state(
        store,
        host,
        call,
        project,
        database,
        DesiredState::Suspended,
    )
}
pub fn resume(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    set_state(store, host, call, project, database, DesiredState::Running)
}

/// The branch of the database a connection request names: its first by default.
fn branch_of(
    store: &Store,
    record: &DatabaseRecord,
    named: Option<&str>,
) -> Result<BranchId, Problem> {
    let Some(named) = named else {
        return Ok(record.id);
    };
    let unknown = || Problem::not_found("There is no such branch in this database.");
    let id: BranchId = internal_id("br", named)
        .and_then(|id| id.parse().ok())
        .ok_or_else(unknown)?;
    match store.database_of(record.project, id) {
        Ok(Some(root)) if root == record.id => Ok(id),
        _ => Err(unknown()),
    }
}
/// Where the branch accepts connections, with its password: the same reply
/// the command line's `connect` gets. `None` where PostgreSQL is private.
fn target(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    record: &DatabaseRecord,
    branch: BranchId,
) -> Result<Option<Value>, Problem> {
    let governed = store
        .governed_branch(branch)
        .map_err(|e| Problem::from(&e))?;
    if governed || matches!(call.caller, Caller::Principal(_)) {
        return Ok(None);
    }
    let stopped = || Problem::unavailable("The database engines are not running.");
    let host = owner(host)?;
    let (cell, gateway) = match (host.cell, host.gateway.as_deref_mut()) {
        (Some(cell), Some(gateway)) => (cell, gateway),
        _ => return Err(stopped()),
    };
    store
        .accepting_work(branch)
        .map_err(|e| refused("conflict", &e))?;
    gateway
        .ensure_listener(store, cell, branch)
        .map_err(|e| Problem::from(&e))?;
    store
        .stable_connection_json(record.project, branch)
        .map(Some)
        .map_err(|e| Problem::from(&e))
}

pub fn connection(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (_, record) = found(store, project, database)?;
    let branch = branch_of(store, &record, call.parameter("branch")?)?;
    let id = public_id("br", &branch.to_string());
    Ok(Reply::ok(
        match target(store, host, call, &record, branch)? {
            None => json!({"available": false, "branch_id": id}),
            Some(found) => json!({
                "available": true,
                "branch_id": id,
                "host": found["host"],
                "port": found["port"],
                "database": found["database"],
                "role": found["username"],
                "sslmode": "prefer",
            }),
        },
    ))
}

pub fn credentials(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (_, record) = found(store, project, database)?;
    let named = match &call.body {
        None => None,
        Some(_) => text(fields(call, &["branch_id"])?, "branch_id")?,
    };
    let branch = branch_of(store, &record, named)?;
    let found = target(store, host, call, &record, branch)?.ok_or_else(|| {
        Problem::new(
            409,
            "conflict",
            "PostgreSQL is private in this project; nobody connects to it directly.",
        )
    })?;
    let mut reply = Reply::ok(json!({
        "role": found["username"],
        "password": found["password"],
        "uri": found["uri"],
        "expires_at": null,
    }));
    reply
        .headers
        .insert("Cache-Control".into(), "no-store".into());
    Ok(reply)
}
