//! The branches domain. A branch belongs to the database whose first branch
//! is its root, and every change here is the action the command line uses.
use super::{
    Call, Local, Problem, Reply,
    databases::{self, Subject},
    shared::{cursor, etag, fields, idempotent, internal_id, page, public_id, text, timestamp},
};
use crate::api::Action;
use crate::operations::BranchPoint;
use crate::store::{BranchView, DatabaseRecord, ProjectRecord, Store};
use serde_json::{Value, json};
use supabricks_core::resource::{BranchId, DesiredState, OperationId};

fn body_of(view: &BranchView) -> Value {
    json!({
        "id": public_id("br", &view.id.to_string()),
        "database_id": public_id("db", &view.database.to_string()),
        "name": view.name,
        "parent_id": view.parent.map(|parent| public_id("br", &parent.to_string())),
        "state": view.state,
        "is_default": view.is_default,
        "created_from": view
            .from
            .as_deref()
            .and_then(|point| serde_json::from_str::<Value>(point).ok()),
        "created_at": timestamp(view.created_at_ms),
        "expires_at": view.expires_at_ms.map(timestamp),
        "child_count": view.children,
        "connections": {
            "open": view.connections,
            "limit": view.connection_limit,
        },
        "compute":view.compute,
        "storage":view.storage,
        "revision": view.revision,
    })
}
fn missing() -> Problem {
    Problem::not_found("There is no such branch.")
}
/// A branch ID that names a branch of this database.
fn member(store: &Store, database: &DatabaseRecord, branch: &str) -> Result<BranchView, Problem> {
    // An ID of the wrong shape names nothing, exactly like an unknown one.
    let id: BranchId = internal_id("br", branch)
        .and_then(|id| id.parse().ok())
        .ok_or_else(missing)?;
    store
        .branch_view(database.project, database.id, id)
        .map_err(|_| missing())
}
pub(super) fn found(
    store: &Store,
    project: &str,
    database: &str,
    branch: &str,
) -> Result<(ProjectRecord, DatabaseRecord, BranchView), Problem> {
    let (project, database) = databases::found(store, project, database)?;
    let view = member(store, &database, branch)?;
    Ok((project, database, view))
}
fn one(view: &BranchView) -> Reply {
    let mut reply = Reply::ok(body_of(view));
    reply.headers.insert("ETag".into(), etag(view.revision));
    reply
}
fn again(store: &Store, view: &BranchView) -> Result<Reply, Problem> {
    store
        .branch_view(view.project, view.database, view.id)
        .map(|view| one(&view))
        .map_err(|e| Problem::from(&e))
}
fn conflict(detail: &str) -> Problem {
    Problem::new(409, "conflict", detail)
}
fn stale(expected: Option<i64>, view: &BranchView) -> Result<(), Problem> {
    if expected.is_some_and(|revision| revision != view.revision) {
        return Err(Problem::new(
            412,
            "revision_mismatch",
            "The branch has changed; read it again and retry.",
        ));
    }
    Ok(())
}
/// `expires_at` in a body: absent, `null` to never expire, or a future time.
fn expiry(body: &serde_json::Map<String, Value>) -> Result<Option<Option<i64>>, Problem> {
    let wrong = || Problem::invalid("expires_at must be a time in the future, or null.");
    match body.get("expires_at") {
        None => Ok(None),
        Some(Value::Null) => Ok(Some(None)),
        Some(Value::String(text)) => {
            let at = chrono::DateTime::parse_from_rfc3339(text).map_err(|_| wrong())?;
            if at <= chrono::Utc::now() {
                return Err(wrong());
            }
            Ok(Some(Some(at.timestamp_millis())))
        }
        Some(_) => Err(wrong()),
    }
}
/// `from` in a body: where in its parent a new branch starts.
fn point(from: Option<&Value>) -> Result<BranchPoint, Problem> {
    let wrong = || {
        Problem::invalid("from must be {kind: head}, {kind: time, timestamp} or {kind: lsn, lsn}.")
    };
    let Some(from) = from else {
        return Ok(BranchPoint::Head);
    };
    let allowed: &[&str] = match from["kind"].as_str() {
        Some("head") => &["kind"],
        Some("time") => &["kind", "timestamp"],
        Some("lsn") => &["kind", "lsn"],
        _ => return Err(wrong()),
    };
    // The platform's own reading ignores extra fields on `head`.
    if from
        .as_object()
        .is_none_or(|object| object.keys().any(|key| !allowed.contains(&key.as_str())))
    {
        return Err(wrong());
    }
    serde_json::from_value(from.clone()).map_err(|_| wrong())
}
/// A key for a platform request that the API itself repeats at most once.
fn once(purpose: &str) -> String {
    format!("api:{purpose}:{}", OperationId::new())
}

pub fn list(
    store: &mut Store,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (_, database) = databases::found(store, project, database)?;
    let limit = call.limit()?;
    let after = match call.cursor()? {
        None => None,
        Some(position) => match <[String; 2]>::try_from(position) {
            Ok(position) => Some(position),
            Err(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
        },
    };
    let mut views: Vec<_> = store
        .branch_views(database.project, database.id)
        .map_err(|e| Problem::from(&e))?
        .into_iter()
        .filter(|v| {
            after
                .as_ref()
                .is_none_or(|[name, id]| (&v.name, &v.id.to_string()) > (name, id))
        })
        .take(limit + 1)
        .collect();
    let next = (views.len() > limit).then(|| {
        views.truncate(limit);
        let last = views.last().unwrap();
        cursor(&[&last.name, &last.id.to_string()])
    });
    Ok(page(views.iter().map(body_of).collect(), next))
}

pub fn get(
    store: &mut Store,
    project: &str,
    database: &str,
    branch: &str,
) -> Result<Reply, Problem> {
    Ok(one(&found(store, project, database, branch)?.2))
}

pub fn create(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (project, database) = databases::found(store, project, database)?;
    let body = fields(
        call,
        &[
            "name",
            "parent_id",
            "from",
            "expires_at",
            "compute_size",
            "idle_timeout_seconds",
        ],
    )?;
    let name = databases::valid(
        text(body, "name")?.ok_or_else(|| Problem::invalid("name is required."))?,
    )?;
    let parent = match text(body, "parent_id")? {
        None => store
            .branch_view(database.project, database.id, database.id)
            .map_err(|e| Problem::from(&e))?,
        Some(parent) => member(store, &database, parent)
            .map_err(|_| Problem::not_found("There is no such parent branch in this database."))?,
    };
    let settings = super::compute::settings(body, Default::default())?;
    let point = point(body.get("from"))?;
    let expires = expiry(body)?.flatten();
    let host = databases::owner(host)?;
    let scope = format!("{}:createBranch", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:createBranch:{key}");
        // Refuse before anything is written, unless this key already created
        // the branch and is being repeated.
        if !databases::started(store, &native, database.project)? {
            if parent.state == "deleting" {
                return Err(conflict("The branch it starts from is being deleted."));
            }
            if databases::taken(store, &project, &name, None)? {
                return Err(databases::name_taken(&name));
            }
        }
        let made = databases::act(
            store,
            host,
            &project,
            Action::WithCompute {
                creation: Box::new(Action::CreateBranch {
                    name: name.clone(),
                    parent: parent.id.to_string(),
                    key: native,
                    point: point.clone(),
                }),
                settings: settings.clone(),
            },
        )
        .map_err(|e| databases::refused("conflict", &e))?;
        if let Some(at) = expires {
            let branch = made["branch_id"].as_str().ok_or_else(|| {
                Problem::new(500, "internal_error", "The new branch is unreadable.")
            })?;
            // The same key and request on a repeat, so it lands once.
            let expiry = Action::SetTtl {
                branch: branch.to_owned(),
                expected_revision: 1,
                expires_at_ms: Some(at),
                key: format!("api:createBranch:expiry:{key}"),
            };
            databases::act(store, host, &project, expiry)
                .map_err(|e| databases::refused("conflict", &e))?;
        }
        databases::accepted(store, &project, databases::operation_id(&made)?, key)
    })
}

pub fn update(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
    branch: &str,
) -> Result<Reply, Problem> {
    let (project, _, view) = found(store, project, database, branch)?;
    if super::compute::changing(call) {
        return super::compute::update(
            store,
            host,
            call,
            &project,
            view.project,
            view.id,
            view.revision,
        );
    }
    let body = fields(call, &["name", "expires_at"])?;
    if body.is_empty() {
        return Err(Problem::invalid("Give a name or an expiry to change."));
    }
    let name = text(body, "name")?.map(databases::valid).transpose()?;
    let expires = expiry(body)?;
    stale(call.expected_revision(false)?, &view)?;
    let name = name.filter(|name| *name != view.name);
    let expires = expires.filter(|at| *at != view.expires_at_ms);
    if name.is_none() && expires.is_none() {
        return Ok(one(&view));
    }
    if view.state == "deleting" {
        return Err(conflict("The branch is being deleted."));
    }
    // Check everything before changing anything: the two changes are separate.
    if let Some(name) = &name
        && databases::taken(store, &project, name, Some(view.id))?
    {
        return Err(databases::name_taken(name));
    }
    if let Some(Some(_)) = expires {
        if view.parent.is_none() {
            return Err(conflict(
                "A database's first branch cannot expire. Delete the database instead.",
            ));
        }
        if view.is_default {
            return Err(conflict("The project's default branch cannot expire."));
        }
    }
    let host = databases::owner(host)?;
    if let Some(name) = name {
        let rename = Action::RenameBranch {
            branch: view.id.to_string(),
            name,
        };
        databases::act(store, host, &project, rename).map_err(|e| Problem::from(&e))?;
    }
    if let Some(at) = expires {
        let change = Action::SetTtl {
            branch: view.id.to_string(),
            expected_revision: view.lifecycle_revision,
            expires_at_ms: at,
            key: once("updateBranch"),
        };
        databases::act(store, host, &project, change)
            .map_err(|e| databases::refused("conflict", &e))?;
    }
    again(store, &view)
}

pub fn delete(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
    branch: &str,
) -> Result<Reply, Problem> {
    let (project, _, view) = found(store, project, database, branch)?;
    let expected = call.expected_revision(true)?;
    let host = databases::owner(host)?;
    let scope = format!("{}:deleteBranch", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:deleteBranch:{key}");
        if !databases::started(store, &native, view.project)? {
            stale(expected, &view)?;
            if view.state == "deleting" {
                return Err(conflict("The branch is already being deleted."));
            }
            let blocked = |detail: &str| Problem::new(409, "delete_blocked", detail);
            if view.parent.is_none() {
                return Err(blocked(
                    "It is the database's first branch. Delete the database instead.",
                ));
            }
            if view.is_default {
                return Err(blocked(
                    "It is the project's default branch. Make another branch the default first.",
                ));
            }
            if view.children > 0 {
                return Err(blocked("Branches were made from it. Delete them first."));
            }
        }
        let made = databases::change(
            store,
            host,
            &project,
            view.project,
            native,
            "delete_blocked",
            |key| Action::DeleteBranch {
                branch: view.id.to_string(),
                expected_revision: view.lifecycle_revision,
                key,
                force: false,
            },
        )?;
        databases::accepted(store, &project, databases::operation_id(&made)?, key)
    })
}

fn set_state(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    name: &'static str,
    desired: DesiredState,
) -> Result<Reply, Problem> {
    let (project, _, view) = found(store, path.0, path.1, path.2)?;
    let subject = Subject {
        project: &project,
        runtime: view.project,
        id: view.id,
        state: view.state,
        lifecycle_revision: view.lifecycle_revision,
        noun: "branch",
    };
    databases::set_state(store, host, call, &subject, name, desired)
}
pub fn suspend(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
) -> Result<Reply, Problem> {
    let desired = DesiredState::Suspended;
    set_state(store, host, call, path, "suspendBranch", desired)
}
pub fn resume(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
) -> Result<Reply, Problem> {
    let desired = DesiredState::Running;
    set_state(store, host, call, path, "resumeBranch", desired)
}

pub fn make_default(
    store: &mut Store,
    host: &mut Option<Local>,
    project: &str,
    database: &str,
    branch: &str,
) -> Result<Reply, Problem> {
    let (project, _, view) = found(store, project, database, branch)?;
    if view.is_default {
        return Ok(one(&view));
    }
    if view.state == "deleting" {
        return Err(conflict("The branch is being deleted."));
    }
    if view.expires_at_ms.is_some() {
        return Err(conflict(
            "A branch that expires cannot be the default. Remove its expiry first.",
        ));
    }
    let host = databases::owner(host)?;
    let change = Action::SetDefault {
        branch: view.id.to_string(),
        key: once("setDefaultBranch"),
    };
    databases::act(store, host, &project, change)
        .map_err(|e| databases::refused("conflict", &e))?;
    again(store, &view)
}
