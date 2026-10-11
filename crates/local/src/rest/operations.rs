//! The operations domain: one view over every kind of durable work.
use super::{
    Call, Problem, Reply,
    shared::{cursor, internal_id, page, public_id, timestamp},
};
use crate::store::{OperationEntry, ProjectRecord, Store};
use serde_json::{Value, json};

fn steps(names: Vec<String>, next: usize, state: &str) -> Vec<Value> {
    let total = names.len();
    names
        .into_iter()
        .enumerate()
        .map(|(index, name)| {
            let step = if index < next || state == "succeeded" {
                "succeeded"
            } else if index == next && state == "running" {
                "running"
            } else if index == next && state == "failed" {
                "failed"
            } else if matches!(state, "cancelled" | "superseded" | "failed") && index < total {
                "skipped"
            } else {
                "pending"
            };
            json!({"name": name, "state": step})
        })
        .collect()
}
fn failure(detail: &str) -> Value {
    Problem::new(500, "operation_failed", detail).body()
}

/// The API's view of one operation, or `None` if its record has gone.
pub fn view(store: &Store, project: &ProjectRecord, entry: &OperationEntry) -> Option<Value> {
    let mut value = json!({
        "id": public_id("op", &entry.id),
        "project_id": public_id("prj", &project.id),
        "created_at": timestamp(entry.created_at_ms),
        "finished_at": entry.finished_at_ms.map(timestamp),
    });
    let state;
    if entry.source == "apply" {
        let apply = store
            .project_apply(project.id.parse().ok()?, entry.id.parse().ok()?)
            .ok()?;
        state = match apply.state.as_str() {
            "queued" => "pending",
            "preparing" | "activating" => "running",
            "succeeded" => "succeeded",
            "cancelled" => "cancelled",
            _ => "failed",
        };
        value["kind"] = json!("project.apply");
        value["target"] =
            json!({"kind": "project", "id": public_id("prj", &project.id), "label": project.name});
        value["steps"] = json!(steps(
            apply
                .plan
                .steps
                .iter()
                .map(|s| format!("{} {}", s.action, s.logical))
                .collect(),
            apply.next_step,
            state
        ));
        value["cancellable"] = json!(apply.pending() && !apply.cancel_requested);
        if state == "failed" {
            value["error"] = failure(apply.error.as_deref().unwrap_or("The apply failed."));
        }
        if state == "succeeded" {
            value["result"] =
                json!({"project": {"kind": "project", "id": public_id("prj", &project.id)}});
        }
    } else {
        let operation = store.operation(entry.id.parse().ok()?).ok()?;
        let request = store.native_request(&entry.id).ok()?;
        let mutation: Value = serde_json::from_str(&request.request).unwrap_or_default();
        use crate::operations::Status;
        state = match operation.status {
            Status::Succeeded => "succeeded",
            Status::Superseded => "superseded",
            Status::Failed => "failed",
            Status::Pending if request.error.is_some() => "failed",
            Status::Pending if operation.next_step > 0 => "running",
            Status::Pending => "pending",
        };
        let kind = match mutation["kind"].as_str().unwrap_or_default() {
            "create_database" => "database.create".to_owned(),
            "create_branch" | "branch_from" => "branch.create".to_owned(),
            "set_state" => "branch.set_state".to_owned(),
            "set_ttl" => "branch.set_ttl".to_owned(),
            "export" => "analytics.export".to_owned(),
            "" => "branch.change".to_owned(),
            other => format!("branch.{other}"),
        };
        value["kind"] = json!(kind);
        value["target"] =
            json!({"kind": "branch", "id": public_id("br", &operation.branch_id.to_string())});
        value["steps"] = json!(steps(
            operation.steps.iter().map(|s| format!("{s:?}")).collect(),
            operation.next_step,
            state
        ));
        value["cancellable"] = json!(false);
        if state == "failed" {
            value["error"] = failure(request.error.as_deref().unwrap_or("The operation failed."));
        }
    }
    Some(finish(value, state, entry))
}
fn finish(mut value: Value, state: &str, entry: &OperationEntry) -> Value {
    value["state"] = json!(state);
    value["started_at"] = if state == "pending" {
        Value::Null
    } else {
        value["created_at"].clone()
    };
    if let Some(key) = &entry.api_key {
        value["idempotency_key"] = json!(key);
    }
    value
}

fn project(store: &Store, id: &str) -> Result<ProjectRecord, Problem> {
    let missing = || Problem::not_found("There is no such project.");
    store
        .project_record(&internal_id("prj", id).ok_or_else(missing)?)
        .map_err(|_| missing())
}
/// A reply for an operation that is still going asks the client to poll.
fn reply(status: u16, value: Value) -> Reply {
    let mut reply = Reply::ok(value);
    reply.status = status;
    if reply.body.as_ref().unwrap()["finished_at"].is_null() {
        reply.headers.insert("Retry-After".into(), "1".into());
    }
    reply
}
pub fn accepted(project: &ProjectRecord, value: Value) -> Reply {
    let location = format!(
        "{}/projects/{}/operations/{}",
        super::PREFIX,
        public_id("prj", &project.id),
        value["id"].as_str().unwrap_or_default()
    );
    let mut reply = reply(202, value);
    reply.headers.insert("Location".into(), location);
    reply
}

const STATES: [&str; 6] = [
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
];

pub fn list(store: &mut Store, call: &Call, project_id: &str) -> Result<Reply, Problem> {
    let project = project(store, project_id)?;
    let limit = call.limit()?;
    let mut before = match call.cursor()?.as_deref() {
        None => None,
        Some([seq]) => Some(
            seq.parse::<i64>()
                .map_err(|_| Problem::invalid("cursor is not one this server issued."))?,
        ),
        Some(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
    };
    let state = call.parameter("state")?;
    if state.is_some_and(|s| !STATES.contains(&s)) {
        return Err(Problem::invalid("state is not an operation state."));
    }
    let (kind, key, target) = (
        call.parameter("kind")?,
        call.parameter("idempotency_key")?,
        call.parameter("target")?,
    );
    let wanted = |value: &Value| {
        let is = |field: &str| value[field].as_str().unwrap_or_default();
        state.is_none_or(|s| is("state") == s)
            && kind.is_none_or(|k| {
                if k.ends_with('.') {
                    is("kind").starts_with(k)
                } else {
                    is("kind") == k
                }
            })
            && key.is_none_or(|k| is("idempotency_key") == k)
            && target.is_none_or(|t| {
                format!(
                    "{}:{}",
                    value["target"]["kind"].as_str().unwrap_or_default(),
                    value["target"]["id"].as_str().unwrap_or_default()
                ) == t
            })
    };
    // Filters apply after the index scan, so read on until the page is full.
    let (mut items, mut next, mut last) = (Vec::new(), None, None);
    'scan: loop {
        let batch = store
            .operation_entries(&project.id, before, 200)
            .map_err(|e| Problem::from(&e))?;
        if batch.is_empty() {
            break;
        }
        for entry in &batch {
            before = Some(entry.seq);
            let Some(value) = view(store, &project, entry).filter(&wanted) else {
                continue;
            };
            if items.len() == limit {
                // Another match exists, so there is a next page after the last item.
                next = last.map(|seq: i64| cursor(&[&seq.to_string()]));
                break 'scan;
            }
            last = Some(entry.seq);
            items.push(value);
        }
    }
    Ok(page(items, next))
}

fn entry(
    store: &Store,
    project: &ProjectRecord,
    operation: &str,
) -> Result<(OperationEntry, Value), Problem> {
    let missing = || Problem::not_found("There is no such operation in this project.");
    let id = internal_id("op", operation).ok_or_else(missing)?;
    let entry = store
        .operation_entry(&project.id, &id)
        .map_err(|_| missing())?;
    let value = view(store, project, &entry).ok_or_else(missing)?;
    Ok((entry, value))
}

pub fn get(store: &mut Store, project_id: &str, operation: &str) -> Result<Reply, Problem> {
    let project = project(store, project_id)?;
    Ok(reply(200, entry(store, &project, operation)?.1))
}

pub fn cancel(store: &mut Store, project_id: &str, operation: &str) -> Result<Reply, Problem> {
    let project = project(store, project_id)?;
    let (found, value) = entry(store, &project, operation)?;
    if value["cancellable"] != true {
        return Err(Problem::new(
            409,
            "not_cancellable",
            "This operation has finished or cannot be stopped.",
        ));
    }
    let parse = || Problem::new(500, "internal_error", "The operation record is unreadable.");
    let mut apply = store
        .project_apply(
            project.id.parse().map_err(|_| parse())?,
            found.id.parse().map_err(|_| parse())?,
        )
        .map_err(|e| Problem::from(&e))?;
    apply.cancel_requested = true;
    store.save_apply(&apply).map_err(|e| Problem::from(&e))?;
    let value = view(store, &project, &found).ok_or_else(parse)?;
    Ok(accepted(&project, value))
}
