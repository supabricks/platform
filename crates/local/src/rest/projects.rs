//! The projects domain. A project is one instance of a source.
use super::{
    Call, Caller, Problem, Reply, operations,
    shared::{cursor, etag, fields, idempotent, internal_id, page, public_id, text, timestamp},
};
use crate::api::Binding;
use crate::store::{ProjectRecord, Store};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn body_of(caller: Caller, record: &ProjectRecord) -> Value {
    let mut value = json!({
        "id": public_id("prj", &record.id),
        "name": record.name,
        "description": record.description,
        "source_id": public_id("src", &record.source_id),
        "created_at": timestamp(record.created_at_ms),
        "revision": record.revision,
        "counts": {"databases": record.databases, "branches": record.branches},
        // The local owner can do everything and holds no role.
        "my_role": null,
    });
    // A server path is shown only to the owner of the machine.
    if let (Caller::LocalOwner, Some(path)) = (caller, &record.path) {
        value["path"] = json!(path);
    }
    value
}

pub fn list(store: &mut Store, call: &Call) -> Result<Reply, Problem> {
    let limit = call.limit()?;
    let after = call.cursor()?;
    let after = match after.as_deref() {
        None => None,
        Some([name, id]) => Some((name.as_str(), id.as_str())),
        Some(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
    };
    let contains = call.parameter("q")?.filter(|text| !text.is_empty());
    let mut records = store
        .project_records(after, contains, limit + 1)
        .map_err(|e| Problem::from(&e))?;
    let next = (records.len() > limit).then(|| {
        records.truncate(limit);
        let last = records.last().unwrap();
        cursor(&[&last.name, &last.id])
    });
    Ok(page(
        records.iter().map(|r| body_of(call.caller, r)).collect(),
        next,
    ))
}

pub fn get(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    // An ID of the wrong shape names nothing, exactly like an unknown one.
    let missing = || Problem::not_found("There is no such project.");
    let id = internal_id("prj", project).ok_or_else(missing)?;
    let record = store.project_record(&id).map_err(|_| missing())?;
    let mut reply = Reply::ok(body_of(call.caller, &record));
    reply.headers.insert("ETag".into(), etag(record.revision));
    Ok(reply)
}

/// A stable identity derived from an idempotency key, so a repeated create
/// lands on the same project even if the first reply was never stored.
fn derived(purpose: &str, key: &str) -> supabricks_core::resource::OperationId {
    let digest = Sha256::digest(format!("{purpose}:{key}"));
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    // Shaped as a random (version 4) UUID, like every other platform ID.
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    uuid::Uuid::from_bytes(bytes)
        .to_string()
        .parse()
        .expect("a UUID is a valid operation ID")
}

pub fn create(store: &mut Store, host: &Binding, call: &Call) -> Result<Reply, Problem> {
    let body = fields(call, &["name", "description"])?;
    let name = text(body, "name")?.ok_or_else(|| Problem::invalid("name is required."))?;
    let name = supabricks_core::validation::valid_name(&json!({"name": name}), "name")
        .map_err(|e| Problem::invalid(e.message))?;
    let description = text(body, "description")?.unwrap_or_default().to_owned();
    if description.chars().count() > 500 {
        return Err(Problem::invalid(
            "description may be at most 500 characters.",
        ));
    }
    idempotent(store, call, "local-owner:createProject", |store, key| {
        let (source, attempt) = (derived("project", key), derived("apply", key));
        let known = store
            .root()
            .join("console-created-projects")
            .join(source.to_string())
            .exists();
        // Refuse a taken name before anything is written, unless this key
        // already created the project and is being repeated.
        if !known
            && !store
                .project_name_free(&name, None)
                .map_err(|e| Problem::from(&e))?
        {
            return Err(Problem::new(
                409,
                "name_taken",
                format!("A project named {name} already exists. Choose another name."),
            ));
        }
        let created = crate::console::projects::create(store, host, source, &name, attempt)
            .map_err(|e| Problem::from(&e))?;
        let unreadable = || Problem::new(500, "internal_error", "The new project is unreadable.");
        let deployment = created["operation"]["plan"]["context"]["deployment_id"]
            .as_str()
            .ok_or_else(unreadable)?;
        let operation = created["operation"]["id"].as_str().ok_or_else(unreadable)?;
        store
            .set_project_description(deployment, &description)
            .map_err(|e| Problem::from(&e))?;
        store
            .set_operation_api_key(operation, key)
            .map_err(|e| Problem::from(&e))?;
        let project = store
            .project_record(deployment)
            .map_err(|e| Problem::from(&e))?;
        let entry = store
            .operation_entry(deployment, operation)
            .map_err(|e| Problem::from(&e))?;
        let view = operations::view(store, &project, &entry).ok_or_else(unreadable)?;
        Ok(operations::accepted(&project, view))
    })
}

pub fn update(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let missing = || Problem::not_found("There is no such project.");
    let id = internal_id("prj", project).ok_or_else(missing)?;
    store.project_record(&id).map_err(|_| missing())?;
    let body = fields(call, &["name", "description"])?;
    if body.is_empty() {
        return Err(Problem::invalid("Give a name or a description to change."));
    }
    let name = text(body, "name")?
        .map(|name| {
            supabricks_core::validation::valid_name(&json!({"name": name}), "name")
                .map_err(|e| Problem::invalid(e.message))
        })
        .transpose()?;
    let description = text(body, "description")?;
    if description.is_some_and(|text| text.chars().count() > 500) {
        return Err(Problem::invalid(
            "description may be at most 500 characters.",
        ));
    }
    let expected = call.expected_revision(false)?;
    let record = store
        .update_project(&id, name.as_deref(), description, expected)
        .map_err(|e| Problem::from(&e))?;
    let mut reply = Reply::ok(body_of(call.caller, &record));
    reply.headers.insert("ETag".into(), etag(record.revision));
    Ok(reply)
}

fn found(store: &Store, project: &str) -> Result<ProjectRecord, Problem> {
    let missing = || Problem::not_found("There is no such project.");
    store
        .project_record(&internal_id("prj", project).ok_or_else(missing)?)
        .map_err(|_| missing())
}

pub fn deletion_impact(store: &mut Store, project: &str) -> Result<Reply, Problem> {
    let record = found(store, project)?;
    let impact = store
        .project_deletion_impact(&record.id)
        .map_err(|e| Problem::from(&e))?;
    Ok(Reply::ok(json!({
        "removes": {"databases": impact.databases, "branches": impact.branches},
        "blocked_by": impact
            .blockers
            .iter()
            .map(|b| json!({"reason": b.reason, "count": b.count}))
            .collect::<Vec<_>>(),
    })))
}

pub fn delete(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let record = found_or_deleting(store, call, project)?;
    let expected = call.expected_revision(true)?;
    if call.parameter("confirm")? != Some(record.name.as_str()) && !already(store, &record) {
        return Err(Problem::invalid(
            "confirm must be the project's name, exactly as it is now.",
        ));
    }
    idempotent(store, call, "local-owner:deleteProject", |store, key| {
        let operation = derived("delete", key).to_string();
        // `*` matches any revision; a repeat after the delete began matches too.
        let expected = match expected {
            Some(revision) if !already(store, &record) => revision,
            _ => record.revision,
        };
        store
            .delete_project(&record.id, expected, &operation)
            .map_err(|e| Problem::from(&e))?;
        store
            .set_operation_api_key(&operation, key)
            .map_err(|e| Problem::from(&e))?;
        let unreadable = || Problem::new(500, "internal_error", "The deletion is unreadable.");
        let entry = store
            .operation_entry(&record.id, &operation)
            .map_err(|e| Problem::from(&e))?;
        let view = operations::view(store, &record, &entry).ok_or_else(unreadable)?;
        Ok(operations::accepted(&record, view))
    })
}
/// Whether a deletion of this project has already begun.
fn already(store: &Store, record: &ProjectRecord) -> bool {
    store.project_deleted(&record.id).unwrap_or(false)
}
/// The project, even if this same request already began deleting it: a
/// repeat with the same key must reach the stored reply, not a 404.
fn found_or_deleting(store: &Store, call: &Call, project: &str) -> Result<ProjectRecord, Problem> {
    let missing = || Problem::not_found("There is no such project.");
    let id = internal_id("prj", project).ok_or_else(missing)?;
    let record = store.project_record_any(&id).map_err(|_| missing())?;
    if !already(store, &record) {
        return Ok(record);
    }
    let repeated = call.idempotency_key.as_deref().is_some_and(|key| {
        store
            .project_deletion(&id)
            .ok()
            .flatten()
            .is_some_and(|d| d.operation_id == derived("delete", key).to_string())
    });
    if repeated { Ok(record) } else { Err(missing()) }
}
