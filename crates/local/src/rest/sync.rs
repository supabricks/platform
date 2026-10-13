//! The sync domain. A pipeline is what the platform calls a sync policy, and
//! every change here is the command the command line sends.
use super::{
    Call, Local, Problem, Reply, databases, projects,
    shared::{cursor, etag, fields, idempotent, internal_id, page, public_id, timestamp},
};
use crate::store::{Error, ProjectRecord, Store};
use crate::sync::{Command, Config, Continuous, Run, Schedule};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use supabricks_core::error::OperationError;
use supabricks_core::resource::{BranchId, DeploymentId, OperationId, ProjectId};

/// A project and the two IDs the platform knows it by.
struct Place {
    project: ProjectRecord,
    runtime: ProjectId,
    deployment: DeploymentId,
}
fn place(store: &Store, project: &str) -> Result<Place, Problem> {
    let project = projects::found(store, project)?;
    let unreadable = || Problem::new(500, "internal_error", "The project is unreadable.");
    Ok(Place {
        runtime: store
            .project_runtime(&project.id)
            .map_err(|e| Problem::from(&e))?,
        deployment: project.id.parse().map_err(|_| unreadable())?,
        project,
    })
}
fn missing() -> Problem {
    Problem::not_found("There is no such sync pipeline.")
}
fn no_run() -> Problem {
    Problem::not_found("There is no such sync run.")
}
fn conflict(detail: &str) -> Problem {
    Problem::new(409, "conflict", detail)
}
fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
fn time(value: &Value) -> Value {
    value
        .as_i64()
        .map(timestamp)
        .map_or(Value::Null, Value::from)
}
/// The platform's reason for refusing, as a sentence for a person.
fn refused(error: &Error) -> Problem {
    let detail = match error {
        Error::Operation(OperationError::Conflict(detail)) => detail.as_str(),
        Error::Operation(OperationError::InvalidInput(e)) => e.message.as_str(),
        _ => return Problem::from(error),
    };
    match detail {
        "source group already has a snapshot policy" => Problem::new(
            409,
            "pipeline_exists",
            "The branch already has a sync pipeline.",
        ),
        "snapshot policy revision changed or policy deleted" => Problem::new(
            412,
            "revision_mismatch",
            "The pipeline has changed; read it again and retry.",
        ),
        d if d.starts_with("one capture generation is allowed per installation") => Problem::new(
            409,
            "capacity_exhausted",
            "This installation already captures changes from another branch. Delete that pipeline first.",
        ),
        "snapshot policy journal limit reached" => Problem::new(
            409,
            "capacity_exhausted",
            "This installation has recorded as many sync pipelines as it keeps.",
        ),
        "snapshot run journal or queue limit reached" => Problem::new(
            409,
            "capacity_exhausted",
            "This installation has as many sync runs recorded or waiting as it keeps.",
        ),
        "snapshot policy is not active" => conflict("The pipeline is not active."),
        "snapshot policy already has an active run" => {
            conflict("A run of this pipeline is already in progress.")
        }
        "continuous runs are supervised; use pause or resume" => {
            conflict("A continuous pipeline runs by itself. Pause or resume it instead.")
        }
        "published snapshot cannot be cancelled" => {
            conflict("The run succeeded and its version is published.")
        }
        "resync review requires an incremental policy" => {
            conflict("A snapshot pipeline has nothing to resync.")
        }
        "resync review changed; inspect and review again" => {
            conflict("The pipeline has changed since the review. Review again.")
        }
        "snapshot source unavailable, changed lineage, or requires governed authority" => conflict(
            "The branch is gone, is being deleted, or is not the one the pipeline was made for.",
        ),
        _ => Problem::from(error),
    }
}

/// The API's view of a pipeline, from the platform's view of a policy.
fn body_of(store: &Store, at: &Place, view: &Value) -> Value {
    let id = view["id"].as_str().unwrap_or_default();
    let branch = view["branch_id"].as_str().unwrap_or_default();
    let database = branch
        .parse::<BranchId>()
        .ok()
        .and_then(|id| store.database_of(at.runtime, id).ok().flatten());
    let config = &view["config"];
    let continuous = config["mode"] == "continuous";
    let capture = &view["capture_status"];
    let status = &view["continuous_status"];
    json!({
        "id": public_id("sync", id),
        "project_id": public_id("prj", &at.project.id),
        "database_id": database.map(|id| public_id("db", &id.to_string())),
        "branch_id": public_id("br", branch),
        "mode": config["mode"],
        "state": if view["pause_requested"] == true { json!("pausing") } else { view["state"].clone() },
        "error": view["error"],
        "schedule": match &config["schedule"] {
            Value::Null => Value::Null,
            schedule => json!({"interval_seconds": schedule["interval_seconds"]}),
        },
        "continuous": if continuous {
            let set: Continuous = serde_json::from_value(config["continuous"].clone())
                .ok()
                .unwrap_or_default();
            json!({"freshness_ms": set.freshness_ms, "batch_interval_ms": set.batch_interval_ms})
        } else {
            Value::Null
        },
        "limits": config["limits"],
        "created_at": time(&view["created_at_ms"]),
        "next_run_at": time(&view["next_due_at_ms"]),
        "last_success_at": time(&view["last_success_at_ms"]),
        "last_version_id": view["last_epoch_id"].as_str().map(|id| public_id("ver", id)),
        "capture": if capture.is_null() {
            Value::Null
        } else {
            json!({
                "state": capture["state"],
                "error": capture["error"],
                "observed_at": time(&capture["observed_at_ms"]),
                "source_lsn": capture["source_lsn"],
                "captured_lsn": capture["captured_lsn"],
                "published_lsn": capture["published_lsn"],
                "spool_bytes": capture["spool_bytes"],
                "retained_wal_bytes": capture["retained_wal_bytes"],
            })
        },
        "freshness": if status.is_null() {
            Value::Null
        } else {
            json!({
                "state": status["state"],
                "lag_ms": status["oldest_unpublished_commit_age_ms"],
                "observed_at": time(&status["lag_observed_at_ms"]),
                "backlog_bytes": status["backlog_bytes"],
                "pressure": status["pressure"],
            })
        },
        "revision": view["revision"],
    })
}
fn run_of(run: &Run) -> Value {
    json!({
        "id": public_id("run", &run.id.to_string()),
        "pipeline_id": public_id("sync", &run.policy_id.to_string()),
        "branch_id": public_id("br", &run.branch_id.to_string()),
        "trigger": run.trigger,
        "state": run.state,
        "created_at": timestamp(run.admitted_at_ms),
        "scheduled_for": run.scheduled_for_ms.map(timestamp),
        "finished_at": run.finished_at_ms.map(timestamp),
        "version_id": run.epoch_id.as_ref().map(|id| public_id("ver", &id.to_string())),
        "source_lsn": run.source_lsn,
        "batches": run.batches,
        "error": run.error,
    })
}
fn one(store: &Store, at: &Place, view: &Value) -> Reply {
    let mut reply = Reply::ok(body_of(store, at, view));
    reply.headers.insert(
        "ETag".into(),
        etag(view["revision"].as_i64().unwrap_or_default()),
    );
    reply
}
/// Every pipeline of the project that is not deleted, oldest first.
fn views(store: &mut Store, at: &Place) -> Result<Vec<Value>, Problem> {
    let listed = store
        .sync_command(at.runtime, at.deployment, Command::List, now())
        .map_err(|e| Problem::from(&e))?;
    let mut views: Vec<Value> = listed
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|view| view["state"] != "deleted")
        .collect();
    views.sort_by_key(|view| {
        (
            view["created_at_ms"].as_i64().unwrap_or_default(),
            view["id"].as_str().unwrap_or_default().to_owned(),
        )
    });
    Ok(views)
}
/// A pipeline of the project that is not deleted, as (place, ID, view).
fn found(
    store: &mut Store,
    project: &str,
    pipeline: &str,
) -> Result<(Place, OperationId, Value), Problem> {
    let at = place(store, project)?;
    // An ID of the wrong shape names nothing, exactly like an unknown one.
    let id: OperationId = internal_id("sync", pipeline)
        .and_then(|id| id.parse().ok())
        .ok_or_else(missing)?;
    let view = store
        .sync_command(at.runtime, at.deployment, Command::Get { id }, now())
        .map_err(|_| missing())?;
    if view["state"] == "deleted" {
        return Err(missing());
    }
    Ok((at, id, view))
}
fn found_run(store: &Store, project: &str, run: &str) -> Result<(Place, Run), Problem> {
    let at = place(store, project)?;
    let id: OperationId = internal_id("run", run)
        .and_then(|id| id.parse().ok())
        .ok_or_else(no_run)?;
    let run = store.sync_run(at.runtime, id).map_err(|_| no_run())?;
    Ok((at, run))
}
/// The revision `If-Match` must name, checked against the pipeline.
fn expected(call: &Call, view: &Value) -> Result<i64, Problem> {
    let current = view["revision"].as_i64().unwrap_or_default();
    if call
        .expected_revision(true)?
        .is_some_and(|revision| revision != current)
    {
        return Err(Problem::new(
            412,
            "revision_mismatch",
            "The pipeline has changed; read it again and retry.",
        ));
    }
    Ok(current)
}
/// Run a command for the project. The command line reaches the same code
/// through a checkout bound to the project; the API names the project itself.
fn send(store: &mut Store, host: &Local, at: &Place, command: Command) -> Result<Value, Problem> {
    let incremental = match &command {
        Command::Create { config, .. } | Command::Update { config, .. } => config.incremental(),
        Command::Resume { id, .. } => store
            .sync_policy(at.runtime, *id)
            .map_err(|e| Problem::from(&e))?
            .config
            .incremental(),
        _ => false,
    };
    // What the command line checks before it lets changes be applied.
    if incremental && (host.cell.is_none() || !crate::sync::runtime_available(store)) {
        return Err(Problem::unavailable(
            "Triggered and continuous sync need the database engines and the analytical runtime.",
        ));
    }
    store
        .sync_command(at.runtime, at.deployment, command, now())
        .map_err(|e| refused(&e))
}
fn whole(object: &Map<String, Value>, name: &str) -> Result<Option<u64>, Problem> {
    match object.get(name) {
        None => Ok(None),
        Some(value) => value
            .as_u64()
            .map(Some)
            .ok_or_else(|| Problem::invalid(format!("{name} must be a whole number."))),
    }
}
fn object<'a>(
    body: &'a Map<String, Value>,
    name: &str,
    allowed: &[&str],
) -> Result<Option<&'a Map<String, Value>>, Problem> {
    let wrong = || {
        Problem::invalid(format!(
            "{name} must be an object with {}.",
            allowed.join(" or ")
        ))
    };
    match body.get(name) {
        None => Ok(None),
        Some(Value::Object(object))
            if !object.is_empty() && object.keys().all(|key| allowed.contains(&key.as_str())) =>
        {
            Ok(Some(object))
        }
        Some(_) => Err(wrong()),
    }
}
/// Lay what a request sends over a configuration. `strict` is a create,
/// where settings for another mode are a mistake and not a leftover.
fn configured(
    mut config: Config,
    body: &Map<String, Value>,
    strict: bool,
) -> Result<Config, Problem> {
    match body.get("mode") {
        None if strict => return Err(Problem::invalid("mode is required.")),
        None => {}
        Some(Value::String(mode))
            if matches!(mode.as_str(), "snapshot" | "triggered" | "continuous") =>
        {
            config.mode = mode.clone();
            config.strategy = if mode == "snapshot" {
                "full"
            } else {
                "incremental"
            }
            .into();
        }
        Some(_) => {
            return Err(Problem::invalid(
                "mode must be snapshot, triggered or continuous.",
            ));
        }
    }
    match body.get("schedule") {
        None => {}
        Some(Value::Null) => config.schedule = None,
        Some(_) => {
            let schedule = object(body, "schedule", &["interval_seconds"])?
                .ok_or_else(|| Problem::invalid("schedule must be an object."))?;
            config.schedule = Some(Schedule {
                interval_seconds: whole(schedule, "interval_seconds")?
                    .ok_or_else(|| Problem::invalid("interval_seconds is required."))?,
                timezone: "UTC".into(),
                missed_run: "coalesce".into(),
            });
        }
    }
    let sent = object(body, "continuous", &["freshness_ms", "batch_interval_ms"])?;
    if config.mode == "continuous" {
        let mut set = config.continuous.clone().unwrap_or_default();
        if let Some(sent) = sent {
            set.freshness_ms = whole(sent, "freshness_ms")?.unwrap_or(set.freshness_ms);
            set.batch_interval_ms =
                whole(sent, "batch_interval_ms")?.unwrap_or(set.batch_interval_ms);
        }
        config.continuous = Some(set);
    } else if sent.is_some() {
        return Err(Problem::invalid(
            "continuous is only for a continuous pipeline.",
        ));
    } else {
        // A pipeline that leaves continuous mode leaves its settings behind.
        config.continuous = None;
    }
    if let Some(limits) = object(body, "limits", &["max_bytes", "timeout_ms"])? {
        config.limits.max_bytes = whole(limits, "max_bytes")?.unwrap_or(config.limits.max_bytes);
        config.limits.timeout_ms = whole(limits, "timeout_ms")?.unwrap_or(config.limits.timeout_ms);
    }
    config
        .validate()
        .map_err(|e| Problem::from(&e))
        .map(|()| config)
}

pub fn list(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let limit = call.limit()?;
    let wrong = || Problem::invalid("cursor is not one this server issued.");
    let after = match call.cursor()? {
        None => None,
        Some(position) => {
            let [made, id] = <[String; 2]>::try_from(position).map_err(|_| wrong())?;
            Some((made.parse::<i64>().map_err(|_| wrong())?, id))
        }
    };
    let branch = match call.parameter("branch_id")? {
        None => None,
        Some(named) => Some(
            internal_id("br", named)
                .and_then(|id| id.parse::<BranchId>().ok())
                .ok_or_else(|| Problem::invalid("branch_id is not a branch ID."))?
                .to_string(),
        ),
    };
    let mut views: Vec<_> = views(store, &at)?
        .into_iter()
        .filter(|view| {
            branch
                .as_deref()
                .is_none_or(|branch| view["branch_id"] == branch)
        })
        .filter(|view| {
            after.as_ref().is_none_or(|(made, id)| {
                (
                    view["created_at_ms"].as_i64().unwrap_or_default(),
                    view["id"].as_str().unwrap_or_default(),
                ) > (*made, id.as_str())
            })
        })
        .take(limit + 1)
        .collect();
    let next = (views.len() > limit).then(|| {
        views.truncate(limit);
        let last = views.last().unwrap();
        cursor(&[
            &last["created_at_ms"]
                .as_i64()
                .unwrap_or_default()
                .to_string(),
            last["id"].as_str().unwrap_or_default(),
        ])
    });
    Ok(page(
        views.iter().map(|view| body_of(store, &at, view)).collect(),
        next,
    ))
}

pub fn get(store: &mut Store, project: &str, pipeline: &str) -> Result<Reply, Problem> {
    let (at, _, view) = found(store, project, pipeline)?;
    Ok(one(store, &at, &view))
}

pub fn create(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let body = fields(
        call,
        &["branch_id", "mode", "schedule", "continuous", "limits"],
    )?;
    let named = body
        .get("branch_id")
        .ok_or_else(|| Problem::invalid("branch_id is required."))?
        .as_str()
        .ok_or_else(|| Problem::invalid("branch_id must be text."))?;
    let config = configured(Config::default(), body, true)?;
    let unknown = || Problem::not_found("There is no such branch in this project.");
    let branch: BranchId = internal_id("br", named)
        .and_then(|id| id.parse().ok())
        .ok_or_else(unknown)?;
    // The record that holds a backup is not a branch, and has nothing to sync.
    if store
        .database_of(at.runtime, branch)
        .map_err(|e| Problem::from(&e))?
        .is_none()
    {
        return Err(unknown());
    }
    let host = databases::owner(host)?;
    let scope = format!("{}:createSyncPipeline", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let command = Command::Create {
            branch: branch.to_string(),
            key: format!("api:createSyncPipeline:{key}"),
            config: config.clone(),
        };
        let view = send(store, host, &at, command)?;
        let mut reply = one(store, &at, &view);
        reply.status = 201;
        reply.headers.insert(
            "Location".into(),
            format!(
                "{}/projects/{}/sync/pipelines/{}",
                super::PREFIX,
                public_id("prj", &at.project.id),
                public_id("sync", view["id"].as_str().unwrap_or_default())
            ),
        );
        Ok(reply)
    })
}

pub fn update(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    pipeline: &str,
) -> Result<Reply, Problem> {
    let (at, id, view) = found(store, project, pipeline)?;
    let body = fields(call, &["mode", "schedule", "continuous", "limits"])?;
    if body.is_empty() {
        return Err(Problem::invalid(
            "Give a mode, schedule, continuous or limits to change.",
        ));
    }
    let current: Config = serde_json::from_value(view["config"].clone())
        .map_err(|_| Problem::new(500, "internal_error", "The pipeline is unreadable."))?;
    let wanted = configured(current.clone(), body, false)?;
    let host = databases::owner(host)?;
    let scope = format!("{}:updateSyncPipeline", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let revision = expected(call, &view)?;
        if wanted == current {
            return Ok(one(store, &at, &view));
        }
        let command = Command::Update {
            id,
            expected_revision: revision,
            key: format!("api:updateSyncPipeline:{key}"),
            config: wanted.clone(),
        };
        let view = send(store, host, &at, command)?;
        Ok(one(store, &at, &view))
    })
}

/// A change that answers with the pipeline, once per key.
fn changed(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str),
    name: &'static str,
    command: fn(OperationId, i64, String) -> Command,
) -> Result<Reply, Problem> {
    let (at, id, view) = found(store, path.0, path.1)?;
    let host = databases::owner(host)?;
    let scope = format!("{}:{name}:{}", call.caller.scope(), path.1);
    idempotent(store, call, &scope, |store, key| {
        let revision = expected(call, &view)?;
        let view = send(
            store,
            host,
            &at,
            command(id, revision, format!("api:{name}:{key}")),
        )?;
        Ok(one(store, &at, &view))
    })
}
pub fn pause(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str),
) -> Result<Reply, Problem> {
    changed(
        store,
        host,
        call,
        path,
        "pauseSyncPipeline",
        |id, expected_revision, key| Command::Pause {
            id,
            expected_revision,
            key,
        },
    )
}
pub fn resume(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str),
) -> Result<Reply, Problem> {
    changed(
        store,
        host,
        call,
        path,
        "resumeSyncPipeline",
        |id, expected_revision, key| Command::Resume {
            id,
            expected_revision,
            key,
        },
    )
}
pub fn delete(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str),
) -> Result<Reply, Problem> {
    let done = changed(
        store,
        host,
        call,
        path,
        "deleteSyncPipeline",
        |id, expected_revision, key| Command::Delete {
            id,
            expected_revision,
            key,
        },
    )?;
    Ok(Reply {
        status: 204,
        headers: BTreeMap::new(),
        body: None,
        wait: done.wait,
    })
}

pub fn review(store: &mut Store, project: &str, pipeline: &str) -> Result<Reply, Problem> {
    let (at, id, _) = found(store, project, pipeline)?;
    let review = store
        .sync_command(
            at.runtime,
            at.deployment,
            Command::ReviewResync { id },
            now(),
        )
        .map_err(|e| refused(&e))?;
    Ok(Reply::ok(json!({
        "pipeline_id": public_id("sync", &id.to_string()),
        "revision": review["expected_revision"],
        "effect": review["effect"],
        "requires_full_copy": review["requires_full_bootstrap"],
        "deletes_published_versions": review["deletes_published_epochs"],
        "review_hash": review["review_hash"],
    })))
}
pub fn resync(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str),
) -> Result<Reply, Problem> {
    let (at, id, view) = found(store, path.0, path.1)?;
    let body = fields(call, &["review_hash"])?;
    let hash = match body.get("review_hash") {
        Some(Value::String(hash)) if !hash.is_empty() && hash.len() <= 128 => hash.clone(),
        _ => return Err(Problem::invalid("review_hash is required.")),
    };
    let host = databases::owner(host)?;
    let scope = format!("{}:resyncSyncPipeline:{}", call.caller.scope(), path.1);
    idempotent(store, call, &scope, |store, key| {
        let command = Command::Resync {
            id,
            expected_revision: expected(call, &view)?,
            review_hash: hash.clone(),
            key: format!("api:resyncSyncPipeline:{key}"),
        };
        let view = send(store, host, &at, command)?;
        Ok(one(store, &at, &view))
    })
}

pub fn runs(
    store: &mut Store,
    call: &Call,
    project: &str,
    pipeline: &str,
) -> Result<Reply, Problem> {
    let (_, id, _) = found(store, project, pipeline)?;
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
        .sync_runs_page(id, before, limit + 1)
        .map_err(|e| Problem::from(&e))?;
    let next = (rows.len() > limit).then(|| {
        rows.truncate(limit);
        cursor(&[&rows.last().unwrap().0.to_string()])
    });
    Ok(page(
        rows.iter().map(|(_, run)| run_of(run)).collect(),
        next,
    ))
}
pub fn start_run(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    pipeline: &str,
) -> Result<Reply, Problem> {
    let (at, id, view) = found(store, project, pipeline)?;
    let host = databases::owner(host)?;
    let scope = format!("{}:startSyncRun:{pipeline}", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let command = Command::RunNow {
            id,
            expected_revision: view["revision"].as_i64().unwrap_or_default(),
            key: format!("api:startSyncRun:{key}"),
        };
        let made = send(store, host, &at, command)?;
        let run: Run = serde_json::from_value(made)
            .map_err(|_| Problem::new(500, "internal_error", "The run is unreadable."))?;
        let mut reply = Reply::ok(run_of(&run));
        reply.status = 201;
        reply.headers.insert(
            "Location".into(),
            format!(
                "{}/projects/{}/sync/runs/{}",
                super::PREFIX,
                public_id("prj", &at.project.id),
                public_id("run", &run.id.to_string())
            ),
        );
        Ok(reply)
    })
}
pub fn run(store: &mut Store, project: &str, run: &str) -> Result<Reply, Problem> {
    Ok(Reply::ok(run_of(&found_run(store, project, run)?.1)))
}
pub fn cancel_run(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    run: &str,
) -> Result<Reply, Problem> {
    let (at, found) = found_run(store, project, run)?;
    let host = databases::owner(host)?;
    let scope = format!("{}:cancelSyncRun:{run}", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let command = Command::Cancel {
            id: found.id,
            key: format!("api:cancelSyncRun:{key}"),
        };
        let run: Run = serde_json::from_value(send(store, host, &at, command)?)
            .map_err(|_| Problem::new(500, "internal_error", "The run is unreadable."))?;
        Ok(Reply::ok(run_of(&run)))
    })
}

pub fn source(store: &mut Store, project: &str, branch: &str) -> Result<Reply, Problem> {
    let at = place(store, project)?;
    let unknown = || Problem::not_found("There is no such branch in this project.");
    let id: BranchId = internal_id("br", branch)
        .and_then(|id| id.parse().ok())
        .ok_or_else(unknown)?;
    if store
        .database_of(at.runtime, id)
        .map_err(|e| Problem::from(&e))?
        .is_none()
    {
        return Err(unknown());
    }
    let command = Command::Inspect {
        branch: id.to_string(),
    };
    let seen = store
        .sync_command(at.runtime, at.deployment, command, now())
        .map_err(|_| unknown())?;
    let pipeline = views(store, &at)?
        .into_iter()
        .find(|view| view["branch_id"] == id.to_string())
        .map(|view| public_id("sync", view["id"].as_str().unwrap_or_default()));
    Ok(Reply::ok(json!({
        "branch_id": public_id("br", &id.to_string()),
        "available": seen["source_available"],
        "pipeline_id": pipeline,
        "tables_checked": seen["source_qualification"] != "not_probed",
        "requirements": seen["requirements"],
        "modes": ["snapshot", "triggered", "continuous"],
        "continuous_keeps_compute_awake": seen["continuous_keeps_compute_awake"],
    })))
}
