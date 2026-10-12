//! SQL: statements run on a branch through the bounded workers the console
//! uses, and the saved queries a project keeps as files.
use super::{
    Call, Local, PREFIX, Problem, Reply, databases, projects,
    shared::{cursor, etag, fields, idempotent, internal_id, page, public_id, text, timestamp},
};
use crate::{
    console::workspace::{Saved, Target, read_saved, saved_dir},
    query::{Query, WORKERS},
    store::Store,
};
use serde_json::{Map, Value, json};
use std::{collections::BTreeMap, fs, path::PathBuf};
use supabricks_core::resource::{BranchId, OperationId, ProjectId};

/// A project keeps this many saved queries.
const SAVED: usize = 100;
/// The largest saved query file the platform reads back.
const STORED: usize = 65536;

fn runtime(store: &Store, project: &str) -> Result<ProjectId, Problem> {
    let project = projects::found(store, project)?;
    store
        .project_runtime(&project.id)
        .map_err(|e| Problem::from(&e))
}
/// A branch of the project a request names in its body.
fn branch(store: &Store, runtime: ProjectId, named: &str) -> Result<BranchId, Problem> {
    let unknown = || Problem::not_found("There is no such branch in this project.");
    let id: BranchId = internal_id("br", named)
        .and_then(|id| id.parse().ok())
        .ok_or_else(unknown)?;
    store
        .branch_in_project(runtime, id)
        .map_err(|_| unknown())?;
    Ok(id)
}
/// Where the branch stands now, as the platform's saved work records it.
fn target(store: &Store, branch: BranchId) -> Result<Target, Problem> {
    let record = store.branch(branch).map_err(|e| Problem::from(&e))?;
    Ok(Target {
        branch,
        revision: record.revision,
    })
}
/// An ID fixed by the caller and key, so a repeat finds what the first made
/// even when the reply to the first was never stored.
fn keyed(scope: &str, key: &str) -> OperationId {
    use sha2::{Digest, Sha256};
    let hex = hex::encode(Sha256::digest(format!("{scope}\n{key}")));
    internal_id("k", &format!("k_{}", &hex[..32]))
        .and_then(|id| id.parse().ok())
        .expect("32 hexadecimal digits are an ID")
}
fn required<'a>(body: &'a Map<String, Value>, name: &str) -> Result<&'a str, Problem> {
    text(body, name)?.ok_or_else(|| Problem::invalid(format!("{name} is required.")))
}
fn sql_of(text: &str) -> Result<String, Problem> {
    if text.trim().is_empty() || text.len() > 32768 {
        return Err(Problem::invalid(
            "sql must be 1 to 32768 bytes and not only white space.",
        ));
    }
    Ok(text.to_owned())
}
fn created(mut reply: Reply, location: String) -> Reply {
    reply.status = 201;
    reply.headers.insert("Location".into(), location);
    reply
}

// ---- Statements ----

fn no_statement() -> Problem {
    Problem::not_found(
        "There is no such statement. The server forgets a statement ten minutes after it \
         finishes and when it restarts; a write that was running may have committed.",
    )
}
fn scope_of(runtime: ProjectId) -> String {
    format!("api:{runtime}")
}
/// A whole number in a range, or the default when the field is absent.
fn number(
    body: &Map<String, Value>,
    name: &str,
    range: std::ops::RangeInclusive<u64>,
    default: u64,
) -> Result<u64, Problem> {
    match body.get(name) {
        None => Ok(default),
        Some(value) => value.as_u64().filter(|n| range.contains(n)).ok_or_else(|| {
            Problem::invalid(format!(
                "{name} must be a whole number from {} to {}.",
                range.start(),
                range.end()
            ))
        }),
    }
}
/// The platform's record of a statement, as the contract describes it.
fn statement(store: &Store, runtime: ProjectId, value: &Value, asked: (&Query, i64)) -> Value {
    let (query, began_ms) = asked;
    let branch: Option<BranchId> = value["target"]["branch"]
        .as_str()
        .and_then(|id| id.parse().ok());
    // A deleted branch no longer has a database to name; its own ID stands in,
    // as a database's ID is its first branch's.
    let database = branch
        .and_then(|id| store.database_of(runtime, id).ok().flatten().or(Some(id)))
        .map(|id| public_id("db", &id.to_string()));
    let result = match &value["result"] {
        Value::Null => Value::Null,
        result => json!({
            "columns": result["columns"].as_array().map(|columns| columns
                .iter()
                .map(|c| json!({"name": c["name"], "type": c["type"]}))
                .collect::<Vec<_>>()),
            "rows": result["rows"],
            "row_count": result["affected_rows"],
        }),
    };
    let error = match &value["error"] {
        Value::Null => Value::Null,
        error => {
            let message = error["message"].as_str().unwrap_or_default();
            // The platform reports a PostgreSQL error as `SQLSTATE: message`.
            match message.split_once(": ") {
                Some((state, rest)) if error["code"] == "sql_error" => {
                    json!({"sqlstate": state, "message": rest})
                }
                _ => json!({"sqlstate": null, "message": message}),
            }
        }
    };
    json!({
        "id": public_id("stmt", value["id"].as_str().unwrap_or_default()),
        "database_id": database,
        "branch_id": branch.map(|id| public_id("br", &id.to_string())),
        "sql": query.sql,
        "read_only": query.read_only,
        "max_rows": query.max_rows,
        "timeout_ms": query.timeout_ms,
        "state": value["state"],
        "started_at": timestamp(began_ms),
        "elapsed_ms": value["elapsed_ms"],
        "result": result,
        "error": error,
    })
}

pub fn run(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
) -> Result<Reply, Problem> {
    let public = project;
    let runtime = runtime(store, project)?;
    let body = fields(
        call,
        &["branch_id", "sql", "read_only", "max_rows", "timeout_ms"],
    )?;
    let query = Query {
        sql: sql_of(required(body, "sql")?)?,
        read_only: match body.get("read_only") {
            None => true,
            Some(Value::Bool(value)) => *value,
            Some(_) => return Err(Problem::invalid("read_only must be true or false.")),
        },
        max_rows: number(body, "max_rows", 1..=1000, 200)? as usize,
        timeout_ms: number(body, "timeout_ms", 100..=30000, 10000)?,
    };
    let branch = branch(store, runtime, required(body, "branch_id")?)?;
    let host = databases::owner(host)?;
    let scope = scope_of(runtime);
    let replies = format!("{}:runStatement", call.caller.scope());
    idempotent(store, call, &replies, |store, key| {
        let id = keyed(&replies, key);
        let target = target(store, branch)?;
        let stopped = || Problem::unavailable("The database engines are not running.");
        let (Some(cell), Some(gateway), Some(statements)) = (
            host.cell,
            host.gateway.as_deref_mut(),
            host.statements.as_deref_mut(),
        ) else {
            return Err(stopped());
        };
        let existing = statements
            .existing(&scope, id, &target, &query)
            .map_err(|e| Problem::from(&e))?;
        let value = match existing {
            Some(value) => value,
            None => {
                if store
                    .governed_branch(branch)
                    .map_err(|e| Problem::from(&e))?
                {
                    return Err(Problem::new(
                        409,
                        "conflict",
                        "PostgreSQL is private in this project; nobody runs statements on it directly.",
                    ));
                }
                store
                    .accepting_work(branch)
                    .map_err(|e| databases::refused("conflict", &e))?;
                if host.busy + statements.active() >= WORKERS {
                    return Err(Problem::new(
                        429,
                        "capacity_exhausted",
                        "The server is already running four statements. Try again when one finishes.",
                    ));
                }
                gateway
                    .ensure_listener(store, cell, branch)
                    .map_err(|e| Problem::from(&e))?;
                let connection = store
                    .stable_connection_json(runtime, branch)
                    .map_err(|e| Problem::from(&e))?;
                statements
                    .start(
                        scope.clone(),
                        id,
                        target,
                        query.clone(),
                        connection,
                        store.generation(),
                    )
                    .map_err(|e| Problem::from(&e))?
            }
        };
        let asked = statements.asked(&scope, id).ok_or_else(no_statement)?;
        let body = statement(store, runtime, &value, asked);
        let location = format!(
            "{PREFIX}/projects/{public}/statements/{}",
            body["id"].as_str().unwrap_or_default()
        );
        Ok(created(Reply::ok(body), location))
    })
}

fn read(
    store: &mut Store,
    host: &mut Option<Local>,
    project: &str,
    id: &str,
    cancel: bool,
) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let id: OperationId = internal_id("stmt", id)
        .and_then(|id| id.parse().ok())
        .ok_or_else(no_statement)?;
    let statements = databases::owner(host)?
        .statements
        .as_deref_mut()
        .ok_or_else(no_statement)?;
    let scope = scope_of(runtime);
    let value = statements
        .status(&scope, id, cancel)
        .map_err(|_| no_statement())?;
    let asked = statements.asked(&scope, id).ok_or_else(no_statement)?;
    Ok(Reply::ok(statement(store, runtime, &value, asked)))
}
pub fn get(
    store: &mut Store,
    host: &mut Option<Local>,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    read(store, host, project, id, false)
}
pub fn cancel(
    store: &mut Store,
    host: &mut Option<Local>,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    read(store, host, project, id, true)
}

// ---- Saved queries ----

fn no_query() -> Problem {
    Problem::not_found("There is no such saved query.")
}
fn directory(store: &Store, runtime: ProjectId) -> Result<PathBuf, Problem> {
    saved_dir(store.root(), runtime).map_err(|e| Problem::from(&e))
}
fn title_of(text: &str) -> Result<String, Problem> {
    if text.trim().is_empty() || text.chars().count() > 120 || text.chars().any(char::is_control) {
        return Err(Problem::invalid(
            "title must be 1 to 120 characters, not only white space, without control characters.",
        ));
    }
    Ok(text.to_owned())
}
/// A saved query and when its file last changed.
fn saved(path: &std::path::Path) -> Result<(Saved, i64), Problem> {
    let query = read_saved(path).map_err(|e| Problem::from(&e))?;
    let changed = fs::metadata(path)
        .and_then(|m| m.modified())
        .map(|at| chrono::DateTime::<chrono::Utc>::from(at).timestamp_millis())
        .unwrap_or_default();
    Ok((query, changed))
}
fn all(store: &Store, runtime: ProjectId) -> Result<Vec<(Saved, i64)>, Problem> {
    let mut queries = Vec::new();
    for entry in fs::read_dir(directory(store, runtime)?).map_err(|e| Problem::from(&e.into()))? {
        let path = entry.map_err(|e| Problem::from(&e.into()))?.path();
        if path.extension().is_some_and(|e| e == "json") {
            queries.push(saved(&path)?);
        }
    }
    queries
        .sort_by(|(a, _), (b, _)| (&a.title, a.id.to_string()).cmp(&(&b.title, b.id.to_string())));
    Ok(queries)
}
fn found(
    store: &Store,
    project: &str,
    id: &str,
) -> Result<(ProjectId, PathBuf, Saved, i64), Problem> {
    let runtime = runtime(store, project)?;
    let id: OperationId = internal_id("qry", id)
        .and_then(|id| id.parse().ok())
        .ok_or_else(no_query)?;
    let path = directory(store, runtime)?.join(format!("{id}.json"));
    if !path.try_exists().unwrap_or(false) {
        return Err(no_query());
    }
    let (query, changed) = saved(&path)?;
    Ok((runtime, path, query, changed))
}
fn body_of(query: &Saved, changed: i64) -> Value {
    json!({
        "id": public_id("qry", &query.id.to_string()),
        "title": query.title,
        "sql": query.sql,
        "branch_id": public_id("br", &query.target.branch.to_string()),
        "updated_at": timestamp(changed),
        "revision": query.revision,
    })
}
fn one(query: &Saved, changed: i64) -> Reply {
    let mut reply = Reply::ok(body_of(query, changed));
    reply.headers.insert("ETag".into(), etag(query.revision));
    reply
}
fn write(path: &std::path::Path, query: &Saved) -> Result<Reply, Problem> {
    if serde_json::to_vec_pretty(query).map_or(true, |bytes| bytes.len() > STORED) {
        return Err(Problem::invalid(
            "The saved query is too large to store. Shorten the SQL.",
        ));
    }
    crate::supervisor::write_json(path, query).map_err(|e| Problem::from(&e))?;
    let (query, changed) = saved(path)?;
    Ok(one(&query, changed))
}
fn stale(expected: Option<i64>, query: &Saved) -> Result<(), Problem> {
    match expected {
        Some(revision) if revision != query.revision => Err(Problem::new(
            412,
            "revision_mismatch",
            "The saved query has changed since it was read.",
        )),
        _ => Ok(()),
    }
}

pub fn list(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let limit = call.limit()?;
    let after = match call.cursor()? {
        None => None,
        Some(position) => match <[String; 2]>::try_from(position) {
            Ok(position) => Some(position),
            Err(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
        },
    };
    let mut queries: Vec<_> = all(store, runtime)?
        .into_iter()
        .filter(|(q, _)| {
            after
                .as_ref()
                .is_none_or(|[title, id]| (&q.title, &q.id.to_string()) > (title, id))
        })
        .take(limit + 1)
        .collect();
    let next = (queries.len() > limit).then(|| {
        queries.truncate(limit);
        let (last, _) = queries.last().unwrap();
        cursor(&[&last.title, &last.id.to_string()])
    });
    Ok(page(
        queries.iter().map(|(q, at)| body_of(q, *at)).collect(),
        next,
    ))
}

pub fn get_saved(store: &mut Store, project: &str, id: &str) -> Result<Reply, Problem> {
    let (_, _, query, changed) = found(store, project, id)?;
    Ok(one(&query, changed))
}

pub fn create_saved(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let public = project;
    let runtime = runtime(store, project)?;
    let body = fields(call, &["title", "sql", "branch_id"])?;
    let title = title_of(required(body, "title")?)?;
    let sql = sql_of(required(body, "sql")?)?;
    let branch = branch(store, runtime, required(body, "branch_id")?)?;
    let scope = format!("{}:createSavedQuery", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let id = keyed(&format!("{scope}:{runtime}"), key);
        let directory = directory(store, runtime)?;
        let path = directory.join(format!("{id}.json"));
        let location = format!(
            "{PREFIX}/projects/{public}/saved-queries/{}",
            public_id("qry", &id.to_string())
        );
        // This key already saved it, and the reply was lost.
        if path.try_exists().unwrap_or(false) {
            let (query, changed) = saved(&path)?;
            return Ok(created(one(&query, changed), location));
        }
        if all(store, runtime)?.len() >= SAVED {
            return Err(Problem::new(
                409,
                "capacity_exhausted",
                "The project already keeps 100 saved queries. Delete one first.",
            ));
        }
        let query = Saved {
            version: 1,
            id,
            revision: 1,
            target: target(store, branch)?,
            title,
            sql,
        };
        Ok(created(write(&path, &query)?, location))
    })
}

pub fn update_saved(
    store: &mut Store,
    call: &Call,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    let (runtime, path, mut query, changed) = found(store, project, id)?;
    let body = fields(call, &["title", "sql", "branch_id"])?;
    if body.is_empty() {
        return Err(Problem::invalid("Send at least one field to change."));
    }
    let title = text(body, "title")?.map(title_of).transpose()?;
    let sql = text(body, "sql")?.map(sql_of).transpose()?;
    let branch = text(body, "branch_id")?
        .map(|named| branch(store, runtime, named))
        .transpose()?;
    stale(call.expected_revision(false)?, &query)?;
    let before = (query.title.clone(), query.sql.clone(), query.target.branch);
    if let Some(title) = title {
        query.title = title;
    }
    if let Some(sql) = sql {
        query.sql = sql;
    }
    if let Some(branch) = branch.filter(|branch| *branch != query.target.branch) {
        query.target = target(store, branch)?;
    }
    if before == (query.title.clone(), query.sql.clone(), query.target.branch) {
        return Ok(one(&query, changed));
    }
    query.revision += 1;
    write(&path, &query)
}

pub fn delete_saved(
    store: &mut Store,
    call: &Call,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    let (_, path, query, _) = found(store, project, id)?;
    stale(call.expected_revision(false)?, &query)?;
    let sync = |path: &std::path::Path| fs::File::open(path).and_then(|file| file.sync_all());
    fs::remove_file(&path)
        .and_then(|()| path.parent().map_or(Ok(()), sync))
        .map_err(|e| Problem::from(&e.into()))?;
    Ok(Reply {
        status: 204,
        headers: BTreeMap::new(),
        body: None,
        wait: None,
    })
}
