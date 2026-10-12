//! The backups domain. A backup is a point of a branch that storage keeps,
//! held by a record with no compute; restoring one makes a branch from it.
use super::{
    Call, Local, Problem, Reply, branches,
    databases::{self},
    shared::{cursor, fields, idempotent, internal_id, page, public_id, text, timestamp},
};
use crate::api::Action;
use crate::operations::{BranchPoint, Mutation};
use crate::store::{BackupView, DatabaseRecord, ProjectRecord, Store};
use serde_json::{Value, json};
use supabricks_core::resource::{BranchId, DesiredState};

/// The most backups one database holds.
const LIMIT: i64 = 100;

fn body_of(view: &BackupView) -> Value {
    json!({
        "id": public_id("bkp", &view.id.to_string()),
        "database_id": public_id("db", &view.database.to_string()),
        "branch_id": public_id("br", &view.source.to_string()),
        "branch_name": view.source_name,
        "name": view.label,
        "origin": view.origin,
        "state": view.state,
        "created_at": timestamp(view.created_at_ms),
        "lsn": view.lsn.map(|lsn| lsn.to_string()),
        "restored_branch_count": view.restored,
        "error": view.error,
    })
}
fn missing() -> Problem {
    Problem::not_found("There is no such backup.")
}
fn conflict(detail: &str) -> Problem {
    Problem::new(409, "conflict", detail)
}
fn found(
    store: &Store,
    project: &str,
    database: &str,
    backup: &str,
) -> Result<(ProjectRecord, DatabaseRecord, BackupView), Problem> {
    let (project, database) = databases::found(store, project, database)?;
    // An ID of the wrong shape names nothing, exactly like an unknown one.
    let id: BranchId = internal_id("bkp", backup)
        .and_then(|id| id.parse().ok())
        .ok_or_else(missing)?;
    let view = store
        .backup_view(database.project, database.id, id)
        .map_err(|_| missing())?;
    Ok((project, database, view))
}
/// A label: what the contract's `BackupName` allows.
fn label(name: &str) -> Result<String, Problem> {
    if name.is_empty() || name.chars().count() > 64 || name.chars().any(char::is_control) {
        return Err(Problem::invalid(
            "name must be 1 to 64 characters, none of them a control character.",
        ));
    }
    Ok(name.to_owned())
}

pub fn list(
    store: &mut Store,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (_, database) = databases::found(store, project, database)?;
    let limit = call.limit()?;
    let wrong = || Problem::invalid("cursor is not one this server issued.");
    let after = match call.cursor()? {
        None => None,
        Some(position) => {
            let [at, id] = <[String; 2]>::try_from(position).map_err(|_| wrong())?;
            Some((at.parse::<i64>().map_err(|_| wrong())?, id))
        }
    };
    // Newest first, so the next page holds what is older than the last item.
    let mut views: Vec<_> = store
        .backup_views(database.project, database.id)
        .map_err(|e| Problem::from(&e))?
        .into_iter()
        .filter(|v| {
            after
                .as_ref()
                .is_none_or(|(at, id)| (v.created_at_ms, &v.id.to_string()) < (*at, id))
        })
        .take(limit + 1)
        .collect();
    let next = (views.len() > limit).then(|| {
        views.truncate(limit);
        let last = views.last().unwrap();
        cursor(&[&last.created_at_ms.to_string(), &last.id.to_string()])
    });
    Ok(page(views.iter().map(body_of).collect(), next))
}

pub fn get(
    store: &mut Store,
    project: &str,
    database: &str,
    backup: &str,
) -> Result<Reply, Problem> {
    Ok(Reply::ok(body_of(
        &found(store, project, database, backup)?.2,
    )))
}

pub fn create(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (project, database) = databases::found(store, project, database)?;
    let body = fields(call, &["name", "branch_id"])?;
    let name = text(body, "name")?.map(label).transpose()?;
    let source = match text(body, "branch_id")? {
        None => store
            .branch_view(database.project, database.id, database.id)
            .map_err(|e| Problem::from(&e))?,
        Some(branch) => branches::member(store, &database, branch)
            .map_err(|_| Problem::not_found("There is no such branch in this database."))?,
    };
    databases::owner(host)?;
    let scope = format!("{}:createBackup", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:createBackup:{key}");
        // A repeat submits what the first request stored, default name and all.
        let request = match store
            .request_for_key(database.project, &native)
            .map_err(|e| Problem::from(&e))?
        {
            Some(request) => request,
            None => {
                match source.state {
                    "running" | "suspended" => {}
                    "deleting" => return Err(conflict("The branch is being deleted.")),
                    _ => {
                        return Err(conflict(
                            "An earlier change to the branch is still in progress.",
                        ));
                    }
                }
                if database.backups >= LIMIT {
                    return Err(Problem::new(
                        409,
                        "capacity_exhausted",
                        "This database already holds 100 backups. Delete one that is not needed.",
                    ));
                }
                Mutation::Hold {
                    parent_id: source.id,
                    label: name.clone().unwrap_or_else(|| {
                        chrono::Utc::now().format("%Y-%m-%d %H:%M UTC").to_string()
                    }),
                    origin: "manual".into(),
                    timeout_ms: crate::operations::branch_timeout(),
                }
            }
        };
        let operation = store
            .submit(database.project, &native, request)
            .map_err(|e| databases::refused("conflict", &e))?;
        databases::accepted(store, &project, &operation.id.to_string(), key)
    })
}

pub fn delete(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
    backup: &str,
) -> Result<Reply, Problem> {
    let (project, _, view) = found(store, project, database, backup)?;
    databases::owner(host)?;
    let scope = format!("{}:deleteBackup", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:deleteBackup:{key}");
        let request = match store
            .request_for_key(view.project, &native)
            .map_err(|e| Problem::from(&e))?
        {
            Some(request) => request,
            None => {
                if view.state == "deleting" {
                    return Err(conflict("The backup is already being deleted."));
                }
                if view.restored > 0 {
                    return Err(Problem::new(
                        409,
                        "delete_blocked",
                        "A branch was restored from it. Delete that branch first.",
                    ));
                }
                Mutation::SetState {
                    branch_id: view.id,
                    expected_revision: view.lifecycle_revision,
                    desired: DesiredState::Deleted,
                }
            }
        };
        let operation = store
            .submit(view.project, &native, request)
            .map_err(|e| databases::refused("delete_blocked", &e))?;
        databases::accepted(store, &project, &operation.id.to_string(), key)
    })
}

pub fn restore(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
    backup: &str,
) -> Result<Reply, Problem> {
    let (project, _, view) = found(store, project, database, backup)?;
    let body = fields(call, &["name", "expires_at"])?;
    let name = databases::valid(
        text(body, "name")?.ok_or_else(|| Problem::invalid("name is required."))?,
    )?;
    let expires = branches::expiry(body)?.flatten();
    let host = databases::owner(host)?;
    let scope = format!("{}:restoreBackup", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:restoreBackup:{key}");
        if !databases::started(store, &native, view.project)? {
            if view.state != "ready" {
                return Err(conflict(match view.state {
                    "creating" => "The backup is still being taken.",
                    "failed" => "The backup could not be taken and holds nothing.",
                    _ => "The backup is being deleted.",
                }));
            }
            if databases::taken(store, &project, &name, None)? {
                return Err(databases::name_taken(&name));
            }
        }
        let made = databases::change(
            store,
            host,
            &project,
            view.project,
            native,
            "conflict",
            |key| Action::CreateBranch {
                name: name.clone(),
                parent: view.id.to_string(),
                key,
                point: BranchPoint::Head,
            },
        )?;
        if let Some(at) = expires {
            let branch = made["branch_id"].as_str().ok_or_else(|| {
                Problem::new(500, "internal_error", "The new branch is unreadable.")
            })?;
            // The same key and request on a repeat, so it lands once.
            let expiry = Action::SetTtl {
                branch: branch.to_owned(),
                expected_revision: 1,
                expires_at_ms: Some(at),
                key: format!("api:restoreBackup:expiry:{key}"),
            };
            databases::act(store, host, &project, expiry)
                .map_err(|e| databases::refused("conflict", &e))?;
        }
        databases::accepted(store, &project, databases::operation_id(&made)?, key)
    })
}

fn schedule_of(store: &Store, database: &DatabaseRecord) -> Result<Reply, Problem> {
    let policy = store
        .backup_policy(database.id)
        .map_err(|e| Problem::from(&e))?;
    Ok(Reply::ok(match policy {
        None => json!({
            "enabled": false,
            "every_hours": null,
            "keep": null,
            "next_backup_at": null,
            "updated_at": null,
        }),
        Some(policy) => json!({
            "enabled": true,
            "every_hours": policy.every_hours,
            "keep": policy.keep,
            "next_backup_at": timestamp(policy.next_at_ms),
            "updated_at": timestamp(policy.updated_at_ms),
        }),
    }))
}
pub fn schedule(store: &mut Store, project: &str, database: &str) -> Result<Reply, Problem> {
    let (_, database) = databases::found(store, project, database)?;
    schedule_of(store, &database)
}
pub fn set_schedule(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    database: &str,
) -> Result<Reply, Problem> {
    let (_, database) = databases::found(store, project, database)?;
    let body = fields(call, &["enabled", "every_hours", "keep"])?;
    let wrong = || {
        Problem::invalid(
            "Send enabled true with every_hours from 1 to 168 and keep from 1 to 30, or enabled false alone.",
        )
    };
    let whole = |name: &str, most: i64| {
        body.get(name)
            .and_then(Value::as_i64)
            .filter(|n| (1..=most).contains(n))
            .ok_or_else(wrong)
    };
    let wanted = match body.get("enabled") {
        Some(Value::Bool(true)) => Some((whole("every_hours", 168)?, whole("keep", 30)?)),
        Some(Value::Bool(false)) if body.len() == 1 => None,
        _ => return Err(wrong()),
    };
    databases::owner(host)?;
    let current = store
        .backup_policy(database.id)
        .map_err(|e| Problem::from(&e))?
        .map(|policy| (policy.every_hours, policy.keep));
    // The same schedule again changes nothing, not even when it is next due.
    if wanted != current {
        if wanted.is_some() && database.state == "deleting" {
            return Err(conflict("The database is being deleted."));
        }
        store
            .set_backup_policy(database.id, wanted)
            .map_err(|e| Problem::from(&e))?;
    }
    schedule_of(store, &database)
}
