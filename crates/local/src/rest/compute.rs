//! Compute controls use the same revision and durable operation journal as lifecycle changes.
use super::{
    Call, Local, Problem, Reply, databases,
    shared::{fields, idempotent, text},
};
use crate::{
    compute::Settings,
    operations::Mutation,
    store::{ProjectRecord, Store},
};
use supabricks_core::resource::{BranchId, ProjectId};

pub(super) fn settings(
    body: &serde_json::Map<String, serde_json::Value>,
    mut current: Settings,
) -> Result<Settings, Problem> {
    if let Some(size) = text(body, "compute_size")? {
        current.size = size.into();
    }
    if let Some(timeout) = body.get("idle_timeout_seconds") {
        current.idle_timeout_seconds = if timeout.is_null() {
            None
        } else {
            Some(
                timeout
                    .as_u64()
                    .and_then(|n| u32::try_from(n).ok())
                    .ok_or_else(|| {
                        Problem::invalid(
                            "idle_timeout_seconds must be null or an integer from 60 to 86400.",
                        )
                    })?,
            )
        };
    }
    current.validate().map_err(|e| Problem::from(&e))?;
    Ok(current)
}
pub(super) fn changing(call: &Call) -> bool {
    call.body
        .as_ref()
        .and_then(|b| b.as_object())
        .is_some_and(|b| {
            ["compute_size", "idle_timeout_seconds", "allow_restart"]
                .iter()
                .any(|k| b.contains_key(*k))
        })
}
pub(super) fn update(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &ProjectRecord,
    runtime: ProjectId,
    id: BranchId,
    revision: i64,
) -> Result<Reply, Problem> {
    databases::owner(host)?;
    let body = fields(
        call,
        &["compute_size", "idle_timeout_seconds", "allow_restart"],
    )?;
    if !body.contains_key("compute_size") && !body.contains_key("idle_timeout_seconds") {
        return Err(Problem::invalid(
            "Give compute_size or idle_timeout_seconds to change.",
        ));
    }
    let restart = match body.get("allow_restart") {
        None => false,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| Problem::invalid("allow_restart must be a boolean."))?,
    };
    let expected = call.expected_revision(true)?.ok_or_else(|| {
        Problem::invalid("Compute changes require an explicit revision, not If-Match: *.")
    })?;
    let scope = format!("{}:configureCompute", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let native = format!("api:configureCompute:{key}");
        let request = match store
            .request_for_key(runtime, &native)
            .map_err(|e| Problem::from(&e))?
        {
            Some(request) => {
                if !matches!(&request, Mutation::ConfigureCompute { branch_id, expected_revision, settings: previous, allow_restart, api_request }
                    if *branch_id == id && *expected_revision == expected && *allow_restart == restart && *api_request == call.body && settings(body,previous.clone())? == *previous)
                {
                    return Err(Problem::new(
                        409,
                        "idempotency_key_reused",
                        "This key already belongs to a different compute change.",
                    ));
                }
                request
            }
            None => {
                if expected != revision {
                    return Err(Problem::new(
                        412,
                        "revision_mismatch",
                        "The branch has changed; read it again and retry.",
                    ));
                }
                let current = store.compute_settings(id).map_err(|e| Problem::from(&e))?;
                Mutation::ConfigureCompute {
                    branch_id: id,
                    expected_revision: expected,
                    settings: settings(body, current)?,
                    allow_restart: restart,
                    api_request: call.body.clone(),
                }
            }
        };
        let operation = store
            .submit(runtime, &native, request)
            .map_err(|e| databases::refused("compute_change_blocked", &e))?;
        databases::accepted(store, project, &operation.id.to_string(), key)
    })
}
