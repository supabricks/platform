//! Durable publication authority. UC names and properties never confer ownership.
#[cfg(test)]
mod tests;
pub(crate) mod worker;
use crate::{
    api::Binding,
    catalog::{Manager, metadata::Namespace},
    deployments::Context,
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::Path};
use supabricks_core::resource::*;
pub use worker::Service;
const PATH_ESCAPE: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC
    .remove(b'-')
    .remove(b'_')
    .remove(b'.')
    .remove(b'~');

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Preview {
        epoch_id: EpochId,
    },
    Publish {
        epoch_id: EpochId,
        key: String,
        expected_preview: String,
        expected_source_revision: i64,
        expected_binding_revision: i64,
    },
    Status {
        id: OperationId,
    },
    Resolve {
        branch: Option<String>,
    },
    Resume {
        id: OperationId,
    },
    Unpublish {
        id: OperationId,
        key: String,
        expected_binding_revision: i64,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Table {
    pub id: OperationId,
    pub source_schema: String,
    pub source_name: String,
    pub body: Value,
    pub state: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Publication {
    pub id: OperationId,
    pub deployment_id: DeploymentId,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub epoch_id: EpochId,
    pub key: String,
    pub request_hash: String,
    pub preview_hash: String,
    pub source_revision: i64,
    pub expected_binding_revision: i64,
    pub revision: Option<i64>,
    pub namespace: Namespace,
    pub tables: Vec<Table>,
    pub state: String,
    pub error: Option<String>,
    pub unpublish_key: Option<String>,
    pub unpublish_binding_revision: Option<i64>,
    pub snapshot_at_ms: Option<i64>,
    pub manifest_hash: String,
}
fn hash(value: &impl Serialize) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(serde_json::to_vec(value).unwrap()))
}
fn key(value: &str) -> Result<()> {
    if value.is_empty() || value.len() > 128 || value.chars().any(char::is_control) {
        return Err(invalid("publication key requires 1–128 printable bytes"));
    }
    Ok(())
}
fn scoped(store: &mut Store, binding: &Binding) -> Result<Context> {
    binding.validate(store)?;
    if binding.worktree == store.root().join("console-home") {
        return Err(conflict("select a project before publishing"));
    }
    store.binding_context(binding)
}
fn local_namespace(store: &Store, manager: &Manager, owner: &Context) -> Result<Namespace> {
    if manager.status()["mode"] != "local" {
        return Err(conflict(
            "publication requires the qualified managed-local catalog profile",
        ));
    }
    let adapter = manager.adapter(store)?;
    let n = store
        .catalog_namespace(owner.deployment_id, &adapter.provider_id)?
        .ok_or_else(|| conflict("run catalog metadata ensure-namespace for this project first"))?;
    if n.state != "ready" || Some(&n.metastore_id) != adapter.probe.expected_metastore.as_ref() {
        return Err(conflict("catalog namespace identity is not ready"));
    }
    Ok(n)
}
// Only immutable local generation locations from the verified snapshot are accepted.
fn preview(store: &Store, owner: &Context, n: Namespace, epoch: EpochId) -> Result<Value> {
    let s = store.snapshot(owner.runtime_project_id, epoch)?;
    let p = s.publication;
    let branch = store.branch(p.branch_id)?;
    if s.state != "available"
        || branch.expired
        || branch.endpoint.desired_state == DesiredState::Deleted
        || branch.revision != p.source_revision
    {
        return Err(conflict("snapshot/source revision unavailable or changed"));
    }
    let d = p
        .descriptor
        .ok_or_else(|| invalid("snapshot descriptor missing"))?;
    let root = store
        .root()
        .join("analytics/generations")
        .join(p.export_id.to_string());
    crate::analytics::check_ready(&root, &d)?;
    let tables = d["manifest"]["tables"]
        .as_array()
        .ok_or_else(|| invalid("snapshot tables missing"))?;
    if tables.is_empty() || tables.len() > 128 {
        return Err(invalid("publication requires 1–128 tables"));
    }
    let mut names = BTreeSet::new();
    let mut objects = vec![];
    for t in tables {
        let schema = t["schema"]
            .as_str()
            .ok_or_else(|| invalid("missing schema"))?;
        let name = t["name"]
            .as_str()
            .ok_or_else(|| invalid("missing table name"))?;
        if !names.insert((schema.to_lowercase(), name.to_lowercase())) {
            return Err(conflict("case-insensitive table aliases collide"));
        }
        let oid = t["oid"]
            .as_u64()
            .ok_or_else(|| invalid("missing table OID"))?;
        if t["path"] != oid.to_string() || t["version"] != 0 {
            return Err(invalid(
                "publication requires a frozen version-zero Delta table",
            ));
        }
        let path = root.join(oid.to_string());
        let columns = columns(&root, &path, &d, t)?;
        let location = local_location(&path)?;
        let alias = format!("e_{}_{}", epoch.to_string().replace('-', ""), oid);
        objects.push(json!({"source_schema":schema,"source_name":name,"body":{"catalog_name":n.catalog,"schema_name":n.schema,"name":alias,"table_type":"EXTERNAL","data_source_format":"DELTA","storage_location":location,"columns":columns}}));
    }
    let mut v = json!({"api_version":1,"deployment_id":owner.deployment_id,"project_id":owner.runtime_project_id,"branch_id":p.branch_id,"epoch_id":epoch,"source_revision":p.source_revision,"binding_revision":store.catalog_head(owner.deployment_id,p.branch_id)?.0,"namespace":n,"tables":objects,"snapshot_at_ms":p.published_at_ms,"manifest_hash":d["manifest_sha256"],"retention":{"durable":true,"until":"unpublished and references drained","copies":false}});
    if serde_json::to_vec(&v)?.len() > 2 * 1024 * 1024 - 65536 {
        return Err(invalid("publication metadata exceeds 2 MiB response bound"));
    }
    v["preview_hash"] = json!(hash(&v));
    Ok(v)
}
fn local_location(path: &Path) -> Result<String> {
    let canonical = path.canonicalize()?;
    if canonical != path {
        return Err(conflict(
            "snapshot location contains relocated or symlinked components",
        ));
    }
    Ok(format!(
        "file://{}",
        canonical
            .to_str()
            .ok_or_else(|| invalid("non-UTF8 snapshot path"))?
            .split('/')
            .map(|part| percent_encoding::utf8_percent_encode(part, PATH_ESCAPE).to_string())
            .collect::<Vec<_>>()
            .join("/")
    ))
}
pub(crate) fn verify_locations(store: &Store, p: &Publication) -> Result<()> {
    let snapshot = store.snapshot(p.project_id, p.epoch_id)?;
    let d = snapshot
        .publication
        .descriptor
        .ok_or_else(|| conflict("snapshot descriptor missing"))?;
    if snapshot.state != "available" || d["manifest_sha256"] != p.manifest_hash {
        return Err(conflict("snapshot identity changed"));
    }
    let root = store
        .root()
        .join("analytics/generations")
        .join(snapshot.publication.export_id.to_string());
    let tables = d["manifest"]["tables"]
        .as_array()
        .ok_or_else(|| invalid("snapshot tables missing"))?;
    if tables.len() != p.tables.len() {
        return Err(conflict("snapshot table set changed"));
    }
    for (source, t) in tables.iter().zip(&p.tables) {
        let oid = source["oid"]
            .as_u64()
            .ok_or_else(|| invalid("snapshot table OID missing"))?;
        if source["path"] != oid.to_string()
            || source["version"] != 0
            || source["schema"] != t.source_schema
            || source["name"] != t.source_name
            || t.body["columns"] != json!(columns(&root, &root.join(oid.to_string()), &d, source)?)
        {
            return Err(conflict("catalog snapshot schema or Delta version changed"));
        }
        if t.body["storage_location"] != local_location(&root.join(oid.to_string()))? {
            return Err(conflict("catalog publication location changed"));
        }
    }
    Ok(())
}
fn columns(root: &Path, path: &Path, d: &Value, table: &Value) -> Result<Vec<Value>> {
    use sha2::{Digest, Sha256};
    let log = path.join("_delta_log/00000000000000000000.json");
    let meta = std::fs::symlink_metadata(&log)?;
    if !meta.is_file() || meta.len() > 2 * 1024 * 1024 {
        return Err(invalid("invalid bounded Delta log"));
    }
    let bytes = std::fs::read(&log)?;
    let relative = log
        .strip_prefix(root)
        .unwrap()
        .to_str()
        .ok_or_else(|| invalid("invalid path"))?;
    let expected = d["manifest"]["files"]
        .as_array()
        .and_then(|fs| fs.iter().find(|f| f["path"] == relative))
        .ok_or_else(|| invalid("Delta log not in immutable manifest"))?;
    if expected["sha256"] != hex::encode(Sha256::digest(&bytes)) {
        return Err(conflict("Delta schema checksum changed"));
    }
    let mut delta = None;
    for line in bytes.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
        let v: Value = serde_json::from_slice(line)?;
        if let Some(s) = v["metaData"]["schemaString"].as_str() {
            if delta.is_some() {
                return Err(invalid("ambiguous Delta schema"));
            }
            delta = Some(serde_json::from_str::<Value>(s)?);
        }
    }
    let delta = delta.ok_or_else(|| invalid("Delta schema missing"))?;
    let fields = delta["fields"]
        .as_array()
        .ok_or_else(|| invalid("invalid Delta fields"))?;
    let original = table["columns"]
        .as_array()
        .ok_or_else(|| invalid("missing source columns"))?;
    if fields.len() != original.len() || fields.is_empty() || fields.len() > 128 {
        return Err(conflict("source/Delta schema drift"));
    }
    let mut seen = BTreeSet::new();
    let mut out = vec![];
    for (i, (field, source)) in fields.iter().zip(original).enumerate() {
        let name = field["name"]
            .as_str()
            .ok_or_else(|| invalid("missing column name"))?;
        if field["name"] != source["name"]
            || field["nullable"] != source["nullable"]
            || !seen.insert(name.to_lowercase())
        {
            return Err(conflict("source schema drift or column quoting collision"));
        }
        let typ = field["type"]
            .as_str()
            .ok_or_else(|| invalid("unsupported Delta type"))?;
        let kind = match typ {
            "boolean" => "BOOLEAN",
            "short" => "SHORT",
            "integer" => "INT",
            "long" => "LONG",
            "string" => "STRING",
            "date" => "DATE",
            "timestamp" => "TIMESTAMP",
            "timestamp_ntz" => "TIMESTAMP_NTZ",
            s if s.starts_with("decimal(") => "DECIMAL",
            _ => return Err(invalid("unqualified Delta type")),
        };
        out.push(json!({"name":name,"type_name":kind,"type_text":typ,"type_json":field.to_string(),"nullable":field["nullable"],"position":i}));
    }
    Ok(out)
}
pub fn handle(
    store: &mut Store,
    manager: &Manager,
    binding: &Binding,
    command: Command,
) -> Result<Value> {
    let owner = scoped(store, binding)?;
    match &command {
        Command::Status { id } => {
            return Ok(
                json!({"api_version":1,"publication":store.catalog_publication(owner.deployment_id,*id)?}),
            );
        }
        Command::Resolve { branch } => {
            let branch = crate::api::resolve(store, binding, branch.as_deref())?;
            let (revision, id) = store.catalog_head(owner.deployment_id, branch)?;
            let p = id
                .map(|id| store.catalog_publication(owner.deployment_id, id))
                .transpose()?;
            return Ok(
                json!({"api_version":1,"binding_revision":revision,"publication":p.filter(|p|p.state=="published")}),
            );
        }
        Command::Resume { id } => {
            let mut p = store.catalog_publication(owner.deployment_id, *id)?;
            if p.error.is_some() {
                p.error = None;
                store.save_catalog_publication(&p)?;
            }
            return Ok(json!({"api_version":1,"publication":p}));
        }
        Command::Unpublish {
            id,
            key: request,
            expected_binding_revision,
        } => {
            key(request)?;
            let p = store.retire_catalog_publication(
                owner.deployment_id,
                *id,
                request,
                *expected_binding_revision,
            )?;
            return Ok(json!({"api_version":1,"publication":p}));
        }
        _ => (),
    }
    if let Command::Publish { key: request, .. } = &command {
        key(request)?;
        if let Some(p) = store.catalog_publication_key(owner.deployment_id, request)? {
            if p.request_hash != hash(&command) {
                return Err(conflict(
                    "publication request key was reused with different inputs",
                ));
            }
            return Ok(json!({"api_version":1,"publication":p}));
        }
    }
    let n = local_namespace(store, manager, &owner)?;
    let epoch = match &command {
        Command::Preview { epoch_id } | Command::Publish { epoch_id, .. } => *epoch_id,
        _ => unreachable!(),
    };
    let v = preview(store, &owner, n, epoch)?;
    if let Command::Publish {
        key,
        expected_preview,
        expected_source_revision,
        expected_binding_revision,
        ..
    } = &command
    {
        if v["preview_hash"] != *expected_preview
            || v["source_revision"] != *expected_source_revision
            || v["binding_revision"] != *expected_binding_revision
        {
            return Err(conflict(
                "publication preview or source/binding revision changed; preview again",
            ));
        }
        let p = Publication {
            id: OperationId::new(),
            deployment_id: owner.deployment_id,
            project_id: owner.runtime_project_id,
            branch_id: serde_json::from_value(v["branch_id"].clone())?,
            epoch_id: epoch,
            key: key.clone(),
            request_hash: hash(&command),
            preview_hash: expected_preview.clone(),
            source_revision: *expected_source_revision,
            expected_binding_revision: *expected_binding_revision,
            revision: None,
            namespace: serde_json::from_value(v["namespace"].clone())?,
            tables: v["tables"]
                .as_array()
                .unwrap()
                .iter()
                .map(|t| Table {
                    id: OperationId::new(),
                    source_schema: t["source_schema"].as_str().unwrap().into(),
                    source_name: t["source_name"].as_str().unwrap().into(),
                    body: t["body"].clone(),
                    state: "planned".into(),
                })
                .collect(),
            state: "registering".into(),
            error: None,
            unpublish_key: None,
            unpublish_binding_revision: None,
            snapshot_at_ms: v["snapshot_at_ms"].as_i64(),
            manifest_hash: v["manifest_hash"].as_str().unwrap().into(),
        };
        store.begin_catalog_publication(&p)?;
        return Ok(json!({"api_version":1,"publication":p}));
    }
    Ok(v)
}
