//! Source fixtures use the existing bounded ingestion worker and PostgreSQL receipt.
use super::*;
use crate::ingest::{Load, Mapping, State};
use std::io::Write;
fn key(o: &Operation, step: &Step) -> String {
    format!("project:{}:{}", o.plan.context.deployment_id, step.logical)
}
pub(super) fn exists(store: &Store, o: &Operation, step: &Step, branch: BranchId) -> Result<bool> {
    Ok(store
        .ingest_for_key(o.plan.context.runtime_project_id, branch, &key(o, step))?
        .is_some())
}
pub(super) fn load(
    store: &mut Store,
    o: &Operation,
    step: &Step,
    branch: BranchId,
    bytes: &[u8],
) -> Result<Option<Value>> {
    let project = o.plan.context.runtime_project_id;
    let config = step.initialization.as_ref().unwrap();
    let mapping: Mapping = serde_json::from_value(config["mapping"].clone())?;
    let sha = config["sha256"].as_str().unwrap();
    let schema = config["schema"].as_str().unwrap();
    let table = config["table"].as_str().unwrap();
    let key = key(o, step);
    if let Some(job) = store.ingest_for_key(project, branch, &key)? {
        if job.load.source_sha256 != sha
            || job.load.mapping != mapping
            || job.load.schema != schema
            || job.load.table != table
        {
            return Err(conflict(
                "fixture identity already binds different input; use a new logical fixture and fresh destination table",
            ));
        }
        return match job.state {
            State::Succeeded => Ok(Some(
                json!({"boundary":"committed","job":job.id,"sha256":sha,"schema":schema,"table":table,"committed_rows":job.committed_rows}),
            )),
            State::Failed | State::Cancelled => Err(conflict(format!(
                "fixture ingestion {} is {:?}; inspect its receipt/status and explicitly retry a retryable ingestion job before applying again",
                job.id, job.state
            ))),
            _ => Ok(None),
        };
    }
    if !store.ingest_active()?.is_empty() {
        return Ok(None);
    }
    crate::ingest::service::worker(store)?;
    let source = store.acquire_source(
        project,
        step.file.as_ref().unwrap().rsplit('/').next().unwrap(),
    )?;
    let mut file = store.source_writer(project, source.id)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    drop(file);
    let source = store.seal_source(project, source.id)?;
    if source.sha256.as_deref() != Some(sha) {
        return Err(conflict("fixture source changed while staging"));
    }
    store.create_ingest(
        &key,
        Load {
            version: 1,
            project_id: project,
            branch_id: branch,
            branch_revision: store.branch(branch)?.revision,
            source_id: source.id,
            source_sha256: sha.into(),
            mapping,
            schema: schema.into(),
            table: table.into(),
        },
    )?;
    super::checkpoint("fixture_submitted");
    Ok(None)
}
pub(super) fn cancel(store: &mut Store, o: &Operation) -> Result<bool> {
    if let Some(step) = o
        .plan
        .steps
        .get(o.next_step)
        .filter(|s| s.kind == "fixture")
    {
        if let Some(branch) = o
            .resources
            .get(step.database.as_ref().unwrap())
            .and_then(|r| r.branch)
        {
            if let Some(job) =
                store.ingest_for_key(o.plan.context.runtime_project_id, branch, &key(o, step))?
            {
                if matches!(
                    job.state,
                    State::Queued | State::Loading | State::Reconciling
                ) {
                    store.cancel_ingest(job.load.project_id, job.id)?;
                    return Ok(false);
                }
            }
        }
    }
    Ok(true)
}
