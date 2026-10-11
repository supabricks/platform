//! The projects domain. A project is one instance of a source.
use super::{
    Call, Caller, Problem, Reply,
    shared::{cursor, etag, internal_id, page, public_id, timestamp},
};
use crate::store::{ProjectRecord, Store};
use serde_json::{Value, json};

fn body(caller: Caller, record: &ProjectRecord) -> Value {
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
        records.iter().map(|r| body(call.caller, r)).collect(),
        next,
    ))
}

pub fn get(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    // An ID of the wrong shape names nothing, exactly like an unknown one.
    let missing = || Problem::not_found("There is no such project.");
    let id = internal_id("prj", project).ok_or_else(missing)?;
    let record = store.project_record(&id).map_err(|_| missing())?;
    let mut reply = Reply::ok(body(call.caller, &record));
    reply.headers.insert("ETag".into(), etag(record.revision));
    Ok(reply)
}
