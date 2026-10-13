//! Imports: a file sent to the server, inspected by the worker the console
//! uses, and loaded into a new table by the platform's durable import jobs.
use super::{
    Call, Local, PREFIX, Problem, Reply, databases,
    shared::{cursor, fields, idempotent, internal_id, page, public_id, text, timestamp},
    sql::{branch, created, keyed, required, runtime},
};
use crate::{
    api::Binding,
    console::ingestion::{Command, Uploads},
    ingest::{
        self, Column, DataType, Format, Job, JobId, Load, Mapping, SOURCE_BYTES, Source, SourceId,
        State, service::Service,
    },
    store::{Error, Store},
};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use supabricks_core::{
    error::OperationError,
    resource::{BranchId, ProjectId},
};

/// The console session the platform API's uploads are kept under.
const OWNER: &str = "api";

fn no_source() -> Problem {
    Problem::not_found(
        "There is no such import source. The server keeps a source for 24 hours, \
         and deletes one whose upload failed.",
    )
}
fn no_import() -> Problem {
    Problem::not_found("There is no such import.")
}
fn busy(detail: &str) -> Problem {
    Problem::new(429, "capacity_exhausted", detail)
}
/// A platform conflict whose message starts a known way is a fixed capacity.
fn refused(error: &Error) -> Problem {
    let Error::Operation(OperationError::Conflict(message)) = error else {
        return Problem::from(error);
    };
    match message.as_str() {
        m if m.starts_with("a source inspection is already active") => {
            busy("The server inspects one file at a time. Try again when it has finished.")
        }
        m if m.starts_with("upload slots busy") => {
            busy("The server is receiving as many files as it can. Try again shortly.")
        }
        m if m.starts_with("one active import") || m.starts_with("another import is active") => {
            busy("The server runs one import at a time. Try again when it has finished.")
        }
        m if m.starts_with("staging budget exhausted") => Problem::new(
            409,
            "capacity_exhausted",
            "The files waiting on the server have reached 512 MiB. Delete a source, or wait for one to expire.",
        ),
        m if m.starts_with("disk_reserve") => {
            Problem::unavailable("The server does not have enough free disk for the file.")
        }
        _ => Problem::from(error),
    }
}

/// The parts of the owner's console that receive, read and load files.
struct Desk<'a> {
    uploads: &'a mut Uploads,
    service: &'a mut Service,
    binding: Binding,
}
fn desk<'a>(host: &'a mut Local, runtime: ProjectId) -> Result<Desk<'a>, Problem> {
    let worktree = host.binding.worktree.clone();
    match (host.uploads.as_deref_mut(), host.imports.as_deref_mut()) {
        (Some(uploads), Some(service)) => Ok(Desk {
            uploads,
            service,
            binding: Binding {
                project_id: runtime,
                worktree,
            },
        }),
        _ => Err(Problem::unavailable("This server does not import files.")),
    }
}
fn worker(store: &Store) -> Result<(), Problem> {
    ingest::service::worker(store)
        .map(drop)
        .map_err(|_| Problem::unavailable("This installation does not have the import worker."))
}

// ---- How a file is read ----

const READING: [&str; 4] = ["format", "delimiter", "header", "null_strings"];

fn format_name(format: &Format) -> &'static str {
    match format {
        Format::Csv => "csv",
        Format::JsonLines => "json_lines",
        Format::JsonArray => "json_array",
        Format::JsonDocument => "json_document",
        Format::Parquet => "parquet",
    }
}
/// The parser options of a request, with one placeholder column: the worker
/// proposes the real ones.
fn reading(body: &Map<String, Value>) -> Result<Mapping, Problem> {
    let format = match required(body, "format")? {
        "csv" => Format::Csv,
        "json_lines" => Format::JsonLines,
        "json_array" => Format::JsonArray,
        "json_document" => Format::JsonDocument,
        "parquet" => Format::Parquet,
        _ => {
            return Err(Problem::invalid(
                "format must be csv, json_lines, json_array, json_document or parquet.",
            ));
        }
    };
    let header = match body.get("header") {
        None => true,
        Some(Value::Bool(value)) => *value,
        Some(_) => return Err(Problem::invalid("header must be true or false.")),
    };
    let null_strings = match body.get("null_strings") {
        None => Vec::new(),
        Some(value) => serde_json::from_value::<Vec<String>>(value.clone())
            .ok()
            .filter(|values| values.iter().all(|v| v.chars().count() <= 64))
            .ok_or_else(|| {
                Problem::invalid(
                    "null_strings must be a list of text values of at most 64 characters.",
                )
            })?,
    };
    let mapping = Mapping {
        version: ingest::VERSION,
        format,
        delimiter: text(body, "delimiter")?.unwrap_or(",").to_owned(),
        header,
        null_strings,
        columns: vec![Column {
            input: "0".into(),
            name: "probe".into(),
            data_type: DataType::Text,
            nullable: true,
        }],
    };
    mapping.fingerprint().map_err(|_| {
        Problem::invalid(
            "delimiter must be a comma, tab, semicolon or bar; null_strings at most 16 values; \
             and only csv takes a delimiter, header or null_strings other than the defaults.",
        )
    })?;
    Ok(mapping)
}
/// Columns as the contract writes them, from the platform's.
fn columns_out(columns: &Value) -> Value {
    columns
        .as_array()
        .map(|columns| {
            columns
                .iter()
                .map(|c| {
                    json!({
                        "input": c["input"],
                        "name": c["name"],
                        "type": c["data_type"],
                        "nullable": c["nullable"],
                    })
                })
                .collect()
        })
        .unwrap_or(Value::Null)
}
/// The platform's columns, from the contract's.
fn columns_in(value: &Value) -> Result<Vec<Column>, Problem> {
    let wrong = || {
        Problem::invalid(
            "columns must be 1 to 256 objects with input, name, type and nullable; a type is \
             a kind, with precision and scale for decimal only.",
        )
    };
    let list = value
        .as_array()
        .filter(|l| (1..=256).contains(&l.len()))
        .ok_or_else(wrong)?;
    list.iter()
        .map(|column| {
            let object = column.as_object().ok_or_else(wrong)?;
            // The type's own fields are checked here: the platform's reader
            // ignores ones a kind does not take.
            let fields = object
                .get("type")
                .and_then(Value::as_object)
                .ok_or_else(wrong)?;
            let decimal = fields.get("kind").and_then(Value::as_str) == Some("decimal");
            if object.len() != 4 || fields.len() != if decimal { 3 } else { 1 } {
                return Err(wrong());
            }
            serde_json::from_value(json!({
                "input": object.get("input"),
                "name": object.get("name"),
                "data_type": object.get("type"),
                "nullable": object.get("nullable"),
            }))
            .map_err(|_| wrong())
        })
        .collect()
}

// ---- Sources ----

fn source_id(public: &str) -> Result<SourceId, Problem> {
    internal_id("src", public)
        .and_then(|id| id.parse().ok())
        .ok_or_else(no_source)
}
/// A source the server still keeps.
fn kept(store: &Store, runtime: ProjectId, id: SourceId) -> Result<Source, Problem> {
    store
        .ingest_source(runtime, id)
        .ok()
        .filter(|s| matches!(s.state.as_str(), "receiving" | "staged" | "interrupted"))
        .ok_or_else(no_source)
}
/// Whether the file can be inspected and imported. What an inspection found
/// is not kept across a restart, and the platform reads a file again only
/// under the server that received it.
fn ready(store: &Store, source: &Source) -> bool {
    source.state == "staged" && source.generation == store.generation()
}
fn source_body(
    store: &Store,
    desk: &mut Desk,
    source: &Source,
    full: bool,
) -> Result<Value, Problem> {
    let progress = desk.uploads.progress(source.id);
    // Once sealed, the file needs no open upload; free its place.
    if progress.is_some() && source.state != "receiving" {
        desk.uploads.release(source.id);
    }
    let inspecting = desk.service.inspecting(source.id);
    let state = match source.state.as_str() {
        _ if inspecting => "inspecting",
        "receiving" if progress.is_some_and(|(_, _, open)| open) => "receiving",
        _ if ready(store, source) => "ready",
        _ => "failed",
    };
    let status = ingest::service::source_status(store, source.project_id, source.id)
        .map_err(|e| Problem::from(&e))?;
    let error = match (state, status["error"].as_str()) {
        ("receiving" | "inspecting", _) => Value::Null,
        ("failed", _) if source.state == "staged" => json!("server_restarted"),
        (_, Some(code)) => json!(code),
        ("failed", None) => json!("interrupted"),
        _ => Value::Null,
    };
    let (received, size) = match (source.state.as_str(), progress) {
        ("staged", _) if state != "failed" => (source.bytes, Some(source.bytes)),
        ("receiving", Some((received, expected, _))) => (received, Some(expected)),
        _ => (source.bytes, None),
    };
    let mut body = json!({
        "id": public_id("src", &source.id.to_string()),
        "name": source.display_name,
        "state": state,
        "size_bytes": size,
        "received_bytes": received,
        "sha256": source.sha256,
        "error": error,
        "expires_at": timestamp(source.expires_at_ms),
    });
    if full {
        body["inspection"] = match &status["inspection"] {
            Value::Null => Value::Null,
            _ if state != "ready" => Value::Null,
            found => {
                let mapping = &found["mapping"];
                json!({
                    "format": mapping["format"],
                    "delimiter": mapping["delimiter"],
                    "header": mapping["header"],
                    "null_strings": mapping["null_strings"],
                    "columns": columns_out(&mapping["columns"]),
                    "sample_rows": found["rows"],
                    "source_columns": found["source_schema"],
                })
            }
        };
    }
    Ok(body)
}
/// Delete a source and its file, stopping whatever is reading or writing it.
fn dispose(store: &mut Store, desk: &mut Desk, id: SourceId) -> Result<(), Problem> {
    let project = desk.binding.project_id;
    desk.uploads.release(id);
    desk.service
        .cancel_source(store, id)
        .and_then(|()| store.abandon_source(project, id))
        .and_then(|()| store.dispose_source(project, id, false))
        .map_err(|e| Problem::from(&e))
}

pub fn list_sources(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let limit = call.limit()?;
    let after = match call.cursor()? {
        None => None,
        Some(position) => match <[String; 2]>::try_from(position) {
            Ok([at, id]) => Some((
                at.parse::<i64>()
                    .map_err(|_| Problem::invalid("cursor is not one this server issued."))?,
                id,
            )),
            Err(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
        },
    };
    let mut desk = desk(databases::owner(host)?, runtime)?;
    let mut sources: Vec<Source> = store
        .ingest_sources(runtime)
        .map_err(|e| Problem::from(&e))?
        .into_iter()
        .filter(|s| {
            after.as_ref().is_none_or(|(at, id)| {
                (std::cmp::Reverse(s.expires_at_ms), s.id.to_string())
                    > (std::cmp::Reverse(*at), id.clone())
            })
        })
        .take(limit + 1)
        .collect();
    let next = (sources.len() > limit).then(|| {
        sources.truncate(limit);
        let last = sources.last().unwrap();
        cursor(&[&last.expires_at_ms.to_string(), &last.id.to_string()])
    });
    let items = sources
        .iter()
        .map(|s| source_body(store, &mut desk, s, false))
        .collect::<Result<_, _>>()?;
    Ok(page(items, next))
}

pub fn create_source(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
) -> Result<Reply, Problem> {
    let public = project;
    let runtime = runtime(store, project)?;
    let mut allowed = vec!["name", "size_bytes"];
    allowed.extend(READING);
    let body = fields(call, &allowed)?;
    let name = required(body, "name")?;
    if name.is_empty() || name.chars().count() > 255 || name.chars().any(char::is_control) {
        return Err(Problem::invalid(
            "name must be 1 to 255 characters without control characters.",
        ));
    }
    let bytes = body
        .get("size_bytes")
        .and_then(Value::as_u64)
        .filter(|n| (1..=SOURCE_BYTES).contains(n))
        .ok_or_else(|| {
            Problem::invalid("size_bytes must be a whole number from 1 to 104857600.")
        })?;
    let reading = reading(body)?;
    let mut desk = desk(databases::owner(host)?, runtime)?;
    let scope = format!("{}:createImportSource", call.caller.scope());
    // A repeat whose first reply was lost reserves again; the first
    // reservation is released when no content arrives for it.
    idempotent(store, call, &scope, |store, _| {
        worker(store)?;
        let begun = desk
            .uploads
            .handle(
                store,
                desk.service,
                &desk.binding,
                OWNER,
                Command::Begin {
                    name: name.to_owned(),
                    bytes,
                },
            )
            .map_err(|e| refused(&e))?;
        let id: SourceId = serde_json::from_value(begun["source"]["id"].clone())
            .map_err(|_| Problem::new(500, "internal_error", "The source has no ID."))?;
        desk.uploads.read_as(id, reading);
        let source = kept(store, runtime, id)?;
        let body = source_body(store, &mut desk, &source, true)?;
        let location = format!(
            "{PREFIX}/projects/{public}/import-sources/{}",
            body["id"].as_str().unwrap_or_default()
        );
        Ok(created(Reply::ok(body), location))
    })
}

pub fn get_source(
    store: &mut Store,
    host: &mut Option<Local>,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let source = kept(store, runtime, source_id(id)?)?;
    let mut desk = desk(databases::owner(host)?, runtime)?;
    Ok(Reply::ok(source_body(store, &mut desk, &source, true)?))
}

pub fn delete_source(
    store: &mut Store,
    host: &mut Option<Local>,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let source = kept(store, runtime, source_id(id)?)?;
    let mut desk = desk(databases::owner(host)?, runtime)?;
    dispose(store, &mut desk, source.id)?;
    Ok(Reply {
        status: 204,
        headers: BTreeMap::new(),
        body: None,
        wait: None,
    })
}

/// One piece of a source's content, or its end. The transport sends the
/// pieces in order and stops at the first that is refused.
pub fn upload(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let source = kept(store, runtime, source_id(id)?)?;
    let mut desk = desk(databases::owner(host)?, runtime)?;
    let Some(part) = &call.part else {
        return Err(Problem::new(
            415,
            "unsupported_media_type",
            "Send the file's bytes as application/octet-stream.",
        ));
    };
    let id = source.id;
    let (received, expected) = match desk.uploads.progress(id) {
        Some((received, expected, true)) if source.state == "receiving" => (received, expected),
        _ => {
            return Err(Problem::new(
                409,
                "conflict",
                "The source already has its content. A source takes it once.",
            ));
        }
    };
    let pieces = part.hex.len() as u64 / 2;
    if part.offset != received
        || part.offset + pieces > expected
        || (part.last && received != expected)
    {
        dispose(store, &mut desk, id)?;
        return Err(Problem::invalid(format!(
            "The content is not the {expected} bytes the source was created with. \
             The source is deleted; create another."
        )));
    }
    if !part.last {
        // Refuse before anything is stored if the file could not be inspected.
        if received == 0 && desk.service.busy() {
            return Err(busy(
                "The server inspects one file at a time. Try the upload again shortly.",
            ));
        }
        let piece = Command::Chunk {
            source: id,
            offset: part.offset,
            hex: part.hex.clone(),
        };
        let stored = desk
            .uploads
            .handle(store, desk.service, &desk.binding, OWNER, piece);
        return match stored {
            Ok(value) => Ok(Reply::ok(value)),
            Err(error) => {
                let _ = dispose(store, &mut desk, id);
                Err(refused(&error))
            }
        };
    }
    let reading = desk
        .uploads
        .reading(id)
        .ok_or_else(|| Problem::new(500, "internal_error", "The source has no reading."))?;
    let inspect = Command::Inspect {
        source: id,
        mapping: reading,
    };
    if let Err(error) = desk
        .uploads
        .handle(store, desk.service, &desk.binding, OWNER, inspect)
    {
        let _ = dispose(store, &mut desk, id);
        return Err(refused(&error));
    }
    let source = kept(store, runtime, id)?;
    Ok(Reply::ok(source_body(store, &mut desk, &source, true)?))
}

pub fn inspect(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
    id: &str,
) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let source = kept(store, runtime, source_id(id)?)?;
    let reading = reading(fields(call, &READING)?)?;
    let mut desk = desk(databases::owner(host)?, runtime)?;
    let scope = format!("{}:inspectImportSource", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        worker(store)?;
        if !ready(store, &source) {
            return Err(Problem::new(
                409,
                "conflict",
                "Only a source that is ready can be inspected again.",
            ));
        }
        desk.service
            .inspect_uploaded(store, &desk.binding, source.id, reading)
            .map_err(|e| refused(&e))?;
        Ok(Reply::ok(source_body(store, &mut desk, &source, true)?))
    })
}

// ---- Imports ----

fn import_id(public: &str) -> Result<JobId, Problem> {
    internal_id("imp", public)
        .and_then(|id| id.parse().ok())
        .ok_or_else(no_import)
}
fn import_body(store: &Store, runtime: ProjectId, job: &Job) -> Result<Value, Problem> {
    let status = ingest::service::status(store, runtime, job.id).map_err(|e| Problem::from(&e))?;
    let changed = store
        .ingest_updated(runtime, job.id)
        .map_err(|e| Problem::from(&e))?;
    let load = &job.load;
    // A deleted branch no longer has a database to name; its own ID stands in.
    let database = store
        .database_of(runtime, load.branch_id)
        .ok()
        .flatten()
        .unwrap_or(load.branch_id);
    Ok(json!({
        "id": public_id("imp", &job.id.to_string()),
        "source_id": public_id("src", &load.source_id.to_string()),
        "source_name": status["source"]["display_name"],
        "database_id": public_id("db", &database.to_string()),
        "branch_id": public_id("br", &load.branch_id.to_string()),
        "schema": load.schema,
        "table": load.table,
        "format": format_name(&load.mapping.format),
        "columns": columns_out(&json!(load.mapping.columns)),
        "state": job.state,
        "attempts": job.attempt,
        "cancel_requested": job.cancel_requested,
        "parsed_rows": job.parsed_rows,
        "copied_rows": job.copied_rows,
        "committed_rows": job.committed_rows,
        "retryable": job.state == State::Failed && job.retryable && !job.source_released,
        "error": status["error"].as_str(),
        "updated_at": timestamp(changed),
    }))
}
fn found(store: &Store, project: &str, id: &str) -> Result<(ProjectId, Job), Problem> {
    let runtime = runtime(store, project)?;
    let job = store
        .ingest_job(runtime, import_id(id)?)
        .map_err(|_| no_import())?;
    Ok((runtime, job))
}
fn name_of(
    body: &Map<String, Value>,
    field: &str,
    default: Option<&str>,
) -> Result<String, Problem> {
    let name = match (text(body, field)?, default) {
        (Some(name), _) => name,
        (None, Some(default)) => default,
        (None, None) => return Err(Problem::invalid(format!("{field} is required."))),
    };
    ingest::identifier(name)
        .map_err(|_| Problem::invalid(format!("{field} must be 1 to 63 bytes.")))?;
    Ok(name.to_owned())
}

pub fn list(store: &mut Store, call: &Call, project: &str) -> Result<Reply, Problem> {
    let runtime = runtime(store, project)?;
    let limit = call.limit()?;
    let into: Option<BranchId> = match call.parameter("branch_id")? {
        None => None,
        Some(named) => Some(
            internal_id("br", named)
                .and_then(|id| id.parse().ok())
                .ok_or_else(|| Problem::invalid("branch_id is not a branch ID."))?,
        ),
    };
    let wrong = || Problem::invalid("cursor is not one this server issued.");
    let after = match call.cursor()? {
        None => None,
        Some(position) => match <[String; 2]>::try_from(position) {
            Ok([at, id]) => Some((at.parse::<i64>().map_err(|_| wrong())?, id)),
            Err(_) => return Err(wrong()),
        },
    };
    let mut jobs = store
        .ingest_page(runtime, into, after, limit + 1)
        .map_err(|e| Problem::from(&e))?;
    let next = (jobs.len() > limit).then(|| {
        jobs.truncate(limit);
        let (last, at) = jobs.last().unwrap();
        cursor(&[&at.to_string(), &last.id.to_string()])
    });
    let items = jobs
        .iter()
        .map(|(job, _)| import_body(store, runtime, job))
        .collect::<Result<_, _>>()?;
    Ok(page(items, next))
}

pub fn create(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    project: &str,
) -> Result<Reply, Problem> {
    let public = project;
    let runtime = runtime(store, project)?;
    let body = fields(
        call,
        &["source_id", "branch_id", "schema", "table", "columns"],
    )?;
    let into = branch(store, runtime, required(body, "branch_id")?)?;
    let schema = name_of(body, "schema", Some("public"))?;
    let table = name_of(body, "table", None)?;
    let columns = body.get("columns").map(columns_in).transpose()?;
    let source = source_id(required(body, "source_id")?)?;
    let host = databases::owner(host)?;
    let engines = host.cell.is_some();
    let scope = format!("{}:createImport", call.caller.scope());
    idempotent(store, call, &scope, |store, key| {
        let key = keyed(&format!("{scope}:{runtime}"), key).to_string();
        let reply = |store: &Store, job: &Job| {
            let body = import_body(store, runtime, job)?;
            let location = format!(
                "{PREFIX}/projects/{public}/imports/{}",
                body["id"].as_str().unwrap_or_default()
            );
            Ok(created(Reply::ok(body), location))
        };
        // This key already queued it, and the reply was lost.
        if let Some(job) = store
            .ingest_for_key(runtime, into, &key)
            .map_err(|e| Problem::from(&e))?
        {
            return reply(store, &job);
        }
        let source = kept(store, runtime, source)?;
        let status = ingest::service::source_status(store, runtime, source.id)
            .map_err(|e| Problem::from(&e))?;
        let proposed: Option<Mapping> =
            serde_json::from_value(status["inspection"]["mapping"].clone()).ok();
        let (Some(mut mapping), true, Some(sha256)) =
            (proposed, ready(store, &source), source.sha256.clone())
        else {
            return Err(Problem::new(
                409,
                "conflict",
                "The source is not ready, or has no inspection. Inspect it first.",
            ));
        };
        if let Some(columns) = columns {
            mapping.columns = columns;
        }
        if !engines {
            return Err(Problem::unavailable(
                "The database engines are not running.",
            ));
        }
        worker(store)?;
        if store.governed_branch(into).map_err(|e| Problem::from(&e))? {
            return Err(Problem::new(
                409,
                "conflict",
                "PostgreSQL is private in this project; nobody imports into it directly.",
            ));
        }
        match store.connection(runtime, into) {
            Ok(_) => {}
            Err(Error::Operation(OperationError::Unavailable(_))) => {
                return Err(Problem::new(
                    409,
                    "conflict",
                    "The branch is not running. Resume it and try again.",
                ));
            }
            Err(error) => return Err(databases::refused("conflict", &error)),
        }
        let load = Load {
            version: ingest::VERSION,
            project_id: runtime,
            branch_id: into,
            branch_revision: store.branch(into).map_err(|e| Problem::from(&e))?.revision,
            source_id: source.id,
            source_sha256: sha256,
            mapping,
            schema,
            table,
        };
        load.validate().map_err(|e| Problem::from(&e))?;
        let job = store.create_ingest(&key, load).map_err(|e| refused(&e))?;
        reply(store, &job)
    })
}

pub fn get(store: &mut Store, project: &str, id: &str) -> Result<Reply, Problem> {
    let (runtime, job) = found(store, project, id)?;
    Ok(Reply::ok(import_body(store, runtime, &job)?))
}

pub fn cancel(store: &mut Store, project: &str, id: &str) -> Result<Reply, Problem> {
    let (runtime, job) = found(store, project, id)?;
    let job = store
        .cancel_ingest(runtime, job.id)
        .map_err(|e| Problem::from(&e))?;
    Ok(Reply::ok(import_body(store, runtime, &job)?))
}

pub fn retry(store: &mut Store, call: &Call, project: &str, id: &str) -> Result<Reply, Problem> {
    let (runtime, job) = found(store, project, id)?;
    let scope = format!("{}:retryImport", call.caller.scope());
    idempotent(store, call, &scope, |store, _| {
        let job = store
            .retry_ingest(runtime, job.id)
            .map_err(|e| refused(&e))?;
        Ok(Reply::ok(import_body(store, runtime, &job)?))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn object(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }
    #[test]
    fn a_reading_takes_parser_options_for_csv_only() {
        let csv = reading(&object(json!({
            "format": "csv", "delimiter": "\t", "header": false, "null_strings": ["NA", ""]
        })))
        .unwrap();
        assert_eq!(
            (csv.delimiter.as_str(), csv.header, csv.null_strings.len()),
            ("\t", false, 2)
        );
        let json = reading(&object(json!({"format": "json_lines"}))).unwrap();
        assert_eq!((json.delimiter.as_str(), json.header), (",", true));
        for bad in [
            json!({}),
            json!({"format": "tsv"}),
            json!({"format": "csv", "delimiter": ":"}),
            json!({"format": "csv", "delimiter": ""}),
            json!({"format": "csv", "null_strings": [1]}),
            json!({"format": "parquet", "delimiter": ";"}),
            json!({"format": "json_array", "header": false}),
            json!({"format": "json_document", "null_strings": ["x"]}),
        ] {
            assert_eq!(
                reading(&object(bad.clone())).unwrap_err().status,
                400,
                "{bad}"
            );
        }
    }
    #[test]
    fn columns_are_read_strictly_and_written_back_the_same() {
        let sent = json!([
            {"input": "0", "name": "id", "type": {"kind": "bigint"}, "nullable": false},
            {"input": "1", "name": "price", "type": {"kind": "decimal", "precision": 9, "scale": 2}, "nullable": true},
        ]);
        let columns = columns_in(&sent).unwrap();
        assert_eq!(
            columns[1].data_type,
            DataType::Decimal {
                precision: 9,
                scale: 2
            }
        );
        assert_eq!(columns_out(&json!(columns)), sent);
        for bad in [
            json!([]),
            json!("all"),
            json!([{"input": "0", "name": "id", "type": {"kind": "bigint"}}]),
            json!([{"input": "0", "name": "id", "type": "bigint", "nullable": true}]),
            json!([{"input": "0", "name": "id", "type": {"kind": "bigint", "precision": 3}, "nullable": true}]),
            json!([{"input": "0", "name": "id", "type": {"kind": "decimal", "precision": 3}, "nullable": true}]),
            json!([{"input": "0", "name": "id", "type": {"kind": "uuid"}, "nullable": true}]),
            json!([{"input": "0", "name": "id", "type": {"kind": "text"}, "nullable": true, "default": 1}]),
            json!([{"input": 0, "name": "id", "type": {"kind": "text"}, "nullable": true}]),
        ] {
            assert_eq!(columns_in(&bad).unwrap_err().status, 400, "{bad}");
        }
    }
    #[test]
    fn a_fixed_capacity_is_told_apart_from_a_conflict() {
        let conflict = |message: &str| Error::Operation(OperationError::Conflict(message.into()));
        for (message, status) in [
            ("a source inspection is already active", 429),
            ("upload slots busy; dispose an unused source", 429),
            ("one active import per cell", 429),
            ("another import is active", 429),
            (
                "staging budget exhausted; dispose unreferenced sources",
                409,
            ),
            ("disk_reserve: free space below upload reserve", 503),
            ("job cannot be retried", 409),
        ] {
            let problem = refused(&conflict(message));
            assert_eq!(problem.status, status, "{message}");
            assert_eq!(
                problem.code == "capacity_exhausted",
                message != "job cannot be retried" && status != 503,
                "{message}"
            );
        }
    }
}
