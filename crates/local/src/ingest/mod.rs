//! Durable ingestion contracts and the I01 owned CSV service.
pub(crate) mod service;
use crate::store::{Result, error::invalid};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use supabricks_core::resource::{BranchId, OperationId, ProjectId};

pub const VERSION: u32 = 1;
pub const INTERNAL_SCHEMA: &str = "_supabricks";
pub const RECEIPT_DDL: &str = include_str!("receipt.sql");
pub const SOURCE_BYTES: u64 = 100 * 1024 * 1024;
pub const STAGING_BYTES: u64 = 512 * 1024 * 1024;
pub const RETENTION_MS: i64 = 24 * 60 * 60 * 1000;

// Distinct wire identities, independent of display names and filesystem paths.
macro_rules! id {
    ($name:ident) => {
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(pub OperationId);
        impl $name {
            pub fn new() -> Self {
                Self(OperationId::new())
            }
        }
        impl std::str::FromStr for $name {
            type Err = <OperationId as std::str::FromStr>::Err;
            fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
                Ok(Self(s.parse()?))
            }
        }
        impl Default for $name {
            fn default() -> Self {
                Self::new()
            }
        }
        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                self.0.fmt(f)
            }
        }
    };
}
id!(SourceId);
id!(JobId);
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mapping {
    pub version: u32,
    pub format: Format,
    pub delimiter: String,
    pub header: bool,
    pub null_strings: Vec<String>,
    pub columns: Vec<Column>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Format {
    Csv,
    JsonLines,
    JsonArray,
    JsonDocument,
    Parquet,
}
impl Default for Format {
    fn default() -> Self {
        Self::Csv
    }
}
impl Format {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "csv" | "tsv" => Ok(Self::Csv),
            "jsonl" | "ndjson" | "json_lines" => Ok(Self::JsonLines),
            "json" | "json_array" => Ok(Self::JsonArray),
            "json_document" => Ok(Self::JsonDocument),
            "parquet" => Ok(Self::Parquet),
            _ => Err(invalid(
                "format must be csv, tsv, jsonl, json, json_document or parquet",
            )),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Column {
    pub input: String,
    pub name: String,
    pub data_type: DataType,
    pub nullable: bool,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum DataType {
    Text,
    Boolean,
    Smallint,
    Integer,
    Bigint,
    Decimal { precision: u8, scale: u8 },
    Double,
    Date,
    Timestamp,
    TimestampTz,
    Jsonb,
}
pub fn identifier(s: &str) -> Result<()> {
    if s.is_empty() || s.len() > 63 || s.contains('\0') {
        return Err(invalid(
            "identifier must contain 1..63 UTF-8 bytes without NUL",
        ));
    }
    Ok(())
}
impl Mapping {
    pub fn fingerprint(&self) -> Result<String> {
        if self.version != VERSION
            || self.columns.is_empty()
            || self.columns.len() > 256
            || self.delimiter.len() != 1
            || !matches!(self.delimiter.as_bytes()[0], b',' | b'\t' | b';' | b'|')
            || self.null_strings.len() > 16
            || self.null_strings.iter().any(|s| s.len() > 256)
        {
            return Err(invalid("unsupported or unbounded ingestion mapping"));
        }
        if self.format != Format::Csv
            && (self.delimiter != "," || !self.header || !self.null_strings.is_empty())
        {
            return Err(invalid("CSV parser options do not apply to this format"));
        }
        if self.format == Format::Csv && self.columns.iter().any(|c| c.data_type == DataType::Jsonb)
        {
            return Err(invalid("CSV supports scalar mappings only"));
        }
        let mut inputs = std::collections::HashSet::new();
        let mut names = std::collections::HashSet::new();
        for c in &self.columns {
            identifier(&c.name)?;
            if c.input.is_empty()
                || c.input.len() > 1024
                || c.input.contains('\0')
                || !inputs.insert(&c.input)
                || !names.insert(&c.name)
            {
                return Err(invalid("invalid input field or duplicate target column"));
            }
            if let DataType::Decimal { precision, scale } = c.data_type {
                if precision == 0 || precision > 38 || scale > precision {
                    return Err(invalid(
                        "decimal requires 1..38 precision and scale <= precision",
                    ));
                }
            }
        }
        let bytes = serde_json::to_vec(self)?;
        if bytes.len() > 32768 {
            return Err(invalid("mapping exceeds 32 KiB"));
        }
        Ok(hex::encode(Sha256::digest(bytes)))
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Load {
    pub version: u32,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub branch_revision: i64,
    pub source_id: SourceId,
    pub source_sha256: String,
    pub mapping: Mapping,
    pub schema: String,
    pub table: String,
}
impl Load {
    pub fn validate(&self) -> Result<String> {
        identifier(&self.schema)?;
        identifier(&self.table)?;
        if self.version != VERSION
            || self.branch_revision < 1
            || self.schema == INTERNAL_SCHEMA
            || self.schema.starts_with("pg_")
            || self.schema == "information_schema"
            || self.source_sha256.len() != 64
            || !self
                .source_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(invalid("invalid ingestion destination or source hash"));
        }
        self.mapping.fingerprint()
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Inspection {
    pub version: u32,
    pub source_id: SourceId,
    pub source_sha256: String,
    pub mapping: Mapping,
    pub rows: Vec<Vec<Option<String>>>,
    pub sample_only: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_schema: Option<serde_json::Value>,
}
impl Inspection {
    pub fn validate(&self) -> Result<()> {
        self.mapping.fingerprint()?;
        if self.version != VERSION
            || self.source_sha256.len() != 64
            || !self
                .source_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || !self.sample_only
            || self.rows.len() > 100
            || self
                .rows
                .iter()
                .any(|r| r.len() != self.mapping.columns.len())
            || serde_json::to_vec(self)?.len() > 256 * 1024
        {
            return Err(invalid("inspection exceeds bounded sample contract"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Receipt {
    pub version: u32,
    pub origin: String,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub job_id: JobId,
    pub source_sha256: String,
    pub mapping_fingerprint: String,
    pub schema: String,
    pub table: String,
    pub table_oid: u32,
    pub committed_rows: u64,
}
impl Receipt {
    pub fn matches(&self, origin: &str, id: JobId, load: &Load) -> Result<bool> {
        Ok(self.version == VERSION
            && self.origin == origin
            && self.job_id == id
            && self.project_id == load.project_id
            && self.branch_id == load.branch_id
            && self.source_sha256 == load.source_sha256
            && self.mapping_fingerprint == load.validate()?
            && self.schema == load.schema
            && self.table == load.table
            && self.table_oid > 0
            && self.committed_rows <= i64::MAX as u64)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Queued,
    Loading,
    Reconciling,
    Succeeded,
    Failed,
    Cancelled,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub id: SourceId,
    pub project_id: ProjectId,
    pub generation: i64,
    pub display_name: String,
    pub state: String,
    pub bytes: u64,
    pub sha256: Option<String>,
    pub expires_at_ms: i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Job {
    pub id: JobId,
    pub load: Load,
    pub state: State,
    pub attempt: u32,
    pub generation: i64,
    #[serde(skip_serializing)]
    pub worker: Option<crate::supervisor::OwnedProcess>,
    pub cancel_requested: bool,
    pub parsed_rows: u64,
    pub copied_rows: u64,
    pub committed_rows: Option<u64>,
    pub retryable: bool,
    pub source_released: bool,
}
/// A worker receives immutable input and an owned-process ticket, never SQLite.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerInput {
    pub version: u32,
    pub job: JobId,
    pub attempt: u32,
    pub generation: i64,
    pub origin: String,
    pub load: Load,
}
/// EOF, connection errors and a missing response are Unknown, never Absent.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case", deny_unknown_fields)]
pub enum Reconciliation {
    Committed { receipt: Receipt, target_oid: u32 },
    Absent { target_exists: bool },
    Unknown,
}

/// Fence surviving ingestion processes before marking interrupted state. Reused
/// by startup and shutdown; queued work is never automatically replayed.
pub(crate) fn recover(store: &mut crate::store::Store) -> Result<()> {
    for process in store
        .native_processes()?
        .into_iter()
        .filter(|p| p.role.starts_with("ingest-"))
    {
        crate::supervisor::stop(&process)?;
        store.forget_native_process(&process)?;
    }
    store.interrupt_ingest()?;
    let tmp = store.root().join("tmp");
    if tmp.exists() {
        if !std::fs::symlink_metadata(&tmp)?.is_dir() {
            return Err(crate::store::error::conflict("invalid temporary workspace"));
        }
        for entry in std::fs::read_dir(&tmp)? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            let id = name
                .strip_prefix("ingest-source-")
                .or_else(|| name.strip_prefix("ingest-check-"))
                .or_else(|| name.strip_prefix("ingest-"));
            if id.is_some_and(|s| s.parse::<OperationId>().is_ok()) && entry.file_type()?.is_dir() {
                std::fs::remove_dir_all(entry.path())?;
            }
        }
    }
    Ok(())
}
