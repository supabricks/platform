//! PK08: bounded PostgreSQL logical table sets, without executable dump SQL.
mod postgres;

use crate::store::{
    Result,
    error::{conflict, invalid},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet, fs::OpenOptions, io::Read, os::unix::fs::OpenOptionsExt, path::Path,
};
use supabricks_core::resource::{BranchId, ProjectId};

pub const MAX_DATA: usize = 32 * 1024 * 1024;
pub const MAX_ARCHIVE: usize = 66 * 1024 * 1024;
pub const MAX_ROW: usize = 256 * 1024;
pub const MAX_ROWS: u64 = 100_000;
pub const MAX_TABLES: usize = 16;
pub const MAX_COLUMNS: usize = 64;
pub const TIMEOUT_SECONDS: u64 = 300;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(deny_unknown_fields)]
pub struct TableName {
    pub schema: String,
    pub name: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Selection {
    pub version: u32,
    pub tables: Vec<TableName>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Provenance {
    pub definition_id: ProjectId,
    pub source_sha256: String,
    pub runtime_project_id: ProjectId,
    pub branch_id: BranchId,
    pub branch_revision: i64,
    pub snapshot: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Column {
    pub name: String,
    pub data_type: Type,
    pub nullable: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Type {
    Boolean,
    Smallint,
    Integer,
    Bigint,
    Text,
    Bytea,
    Uuid,
    Json,
    Jsonb,
    Real,
    Double,
    Date,
    Timestamp,
    TimestampTz,
    Varchar {
        length: Option<u32>,
    },
    Numeric {
        precision: Option<u16>,
        scale: Option<i16>,
    },
}
impl Type {
    pub fn sql(&self) -> Result<String> {
        Ok(match self {
            Self::Boolean => "pg_catalog.bool".into(),
            Self::Smallint => "pg_catalog.int2".into(),
            Self::Integer => "pg_catalog.int4".into(),
            Self::Bigint => "pg_catalog.int8".into(),
            Self::Text => "pg_catalog.text".into(),
            Self::Bytea => "pg_catalog.bytea".into(),
            Self::Uuid => "pg_catalog.uuid".into(),
            Self::Json => "pg_catalog.json".into(),
            Self::Jsonb => "pg_catalog.jsonb".into(),
            Self::Real => "pg_catalog.float4".into(),
            Self::Double => "pg_catalog.float8".into(),
            Self::Date => "pg_catalog.date".into(),
            Self::Timestamp => "pg_catalog.timestamp".into(),
            Self::TimestampTz => "pg_catalog.timestamptz".into(),
            Self::Varchar { length: None } => "pg_catalog.varchar".into(),
            Self::Varchar { length: Some(n) } if (1..=10_485_760).contains(n) => {
                format!("pg_catalog.varchar({n})")
            }
            Self::Numeric {
                precision: None,
                scale: None,
            } => "pg_catalog.numeric".into(),
            Self::Numeric {
                precision: Some(p),
                scale: Some(s),
            } if (1..=1000).contains(p) && (-1000..=1000).contains(s) => {
                format!("pg_catalog.numeric({p},{s})")
            }
            _ => return Err(invalid("unsupported logical data type modifier")),
        })
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Constraint {
    pub name: String,
    pub kind: ConstraintKind,
    pub columns: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConstraintKind {
    PrimaryKey,
    Unique,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Table {
    pub table: TableName,
    pub columns: Vec<Column>,
    pub constraints: Vec<Constraint>,
    pub rows: u64,
    pub data_sha256: String,
    pub copy_text: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Content {
    pub format_version: u32,
    pub profile: String,
    pub postgres_major: u32,
    pub locale: Locale,
    pub source: Provenance,
    pub tables: Vec<Table>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Locale {
    pub provider: String,
    pub collate: String,
    pub ctype: String,
    pub locale: Option<String>,
    pub version: Option<String>,
}
impl Locale {
    pub fn validate(&self) -> Result<()> {
        let portable = (self.provider == "b"
            && matches!(
                self.locale.as_deref(),
                Some("C" | "C.UTF-8" | "PG_UNICODE_FAST")
            ))
            || (self.provider == "c"
                && matches!(self.collate.as_str(), "C" | "POSIX")
                && matches!(self.ctype.as_str(), "C" | "POSIX")
                && self.locale.is_none()
                && self.version.is_none());
        if !portable
            || self.collate.len() > 128
            || self.ctype.len() > 128
            || self.version.as_ref().is_some_and(|s| s.len() > 128)
        {
            return Err(invalid(
                "logical data requires a portable builtin or C/POSIX database locale",
            ));
        }
        Ok(())
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Archive {
    content: Content,
    content_sha256: String,
}
pub struct Verified {
    pub content: Content,
    pub archive_sha256: String,
    pub content_sha256: String,
}

pub fn hash(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
fn sha(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn identifier(value: &str) -> Result<()> {
    crate::ingest::identifier(value)
}
pub fn quote(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}
impl TableName {
    pub fn validate(&self) -> Result<()> {
        identifier(&self.schema)?;
        identifier(&self.name)?;
        if self.schema.starts_with("pg_")
            || self.schema.starts_with("neon")
            || matches!(self.schema.as_str(), "information_schema" | "_supabricks")
            || (self.schema == "public" && self.name == "health_check")
        {
            return Err(invalid(
                "logical data cannot include system/control relations",
            ));
        }
        Ok(())
    }
    pub fn sql(&self) -> String {
        format!("{}.{}", quote(&self.schema), quote(&self.name))
    }
}
impl Selection {
    pub fn validate(&self) -> Result<()> {
        if self.version != 1 || self.tables.is_empty() || self.tables.len() > MAX_TABLES {
            return Err(invalid("select 1–16 tables with selection version 1"));
        }
        let mut names = BTreeSet::new();
        for table in &self.tables {
            table.validate()?;
            if !names.insert(table) {
                return Err(invalid("duplicate selected table"));
            }
        }
        Ok(())
    }
}
impl Content {
    pub fn validate(&self) -> Result<()> {
        self.locale.validate()?;
        if self.format_version != 1
            || self.profile != "postgres_tables"
            || self.postgres_major != 17
            || !sha(&self.source.source_sha256)
            || self.source.branch_revision < 1
            || self.source.snapshot.is_empty()
            || self.source.snapshot.len() > 4096
            || !self
                .source
                .snapshot
                .bytes()
                .all(|b| b.is_ascii_digit() || b":,".contains(&b))
        {
            return Err(invalid(
                "unsupported logical data profile or source provenance",
            ));
        }
        Selection {
            version: 1,
            tables: self.tables.iter().map(|t| t.table.clone()).collect(),
        }
        .validate()?;
        let (mut bytes, mut rows, mut cells) = (0usize, 0u64, 0u64);
        for table in &self.tables {
            if table.columns.is_empty()
                || table.columns.len() > MAX_COLUMNS
                || table.constraints.len() > 64
            {
                return Err(invalid("logical data table exceeds schema limits"));
            }
            let mut names = BTreeSet::new();
            for column in &table.columns {
                identifier(&column.name)?;
                column.data_type.sql()?;
                if !names.insert(&column.name) {
                    return Err(invalid("duplicate column"));
                }
            }
            let mut constraints = BTreeSet::new();
            let mut primary = false;
            for constraint in &table.constraints {
                identifier(&constraint.name)?;
                if !constraints.insert(&constraint.name)
                    || constraint.columns.is_empty()
                    || constraint.columns.len() > MAX_COLUMNS
                    || constraint.columns.iter().collect::<BTreeSet<_>>().len()
                        != constraint.columns.len()
                    || constraint.columns.iter().any(|c| !names.contains(c))
                {
                    return Err(invalid("invalid logical data constraint"));
                }
                if matches!(constraint.kind, ConstraintKind::PrimaryKey) {
                    if primary
                        || constraint
                            .columns
                            .iter()
                            .any(|c| table.columns.iter().any(|a| &a.name == c && a.nullable))
                    {
                        return Err(invalid("invalid primary key"));
                    }
                    primary = true;
                }
            }
            bytes = bytes
                .checked_add(table.copy_text.len())
                .ok_or_else(|| invalid("data size overflow"))?;
            rows = rows
                .checked_add(table.rows)
                .ok_or_else(|| invalid("row count overflow"))?;
            cells = cells
                .checked_add(
                    table
                        .rows
                        .checked_mul(table.columns.len() as u64)
                        .ok_or_else(|| invalid("cell count overflow"))?,
                )
                .ok_or_else(|| invalid("cell count overflow"))?;
            if bytes > MAX_DATA
                || rows > MAX_ROWS
                || cells > 1_000_000
                || hash(table.copy_text.as_bytes()) != table.data_sha256
            {
                return Err(invalid(
                    "logical data exceeds limits or has a changed payload",
                ));
            }
            let mut count = 0;
            for line in table.copy_text.split_inclusive('\n') {
                if !line.ends_with('\n')
                    || line.len() > MAX_ROW
                    || line.as_bytes().contains(&0)
                    || line == "\\.\n"
                    || line.bytes().filter(|b| *b == b'\t').count() + 1 != table.columns.len()
                {
                    return Err(invalid("invalid or oversized COPY text row"));
                }
                count += 1;
            }
            if count != table.rows {
                return Err(invalid("logical data row count differs"));
            }
        }
        Ok(())
    }
}
impl Verified {
    pub fn report(&self) -> Value {
        json!({"api_version":1,"verified":true,"profile":"postgres_tables","format_version":1,"archive_sha256":self.archive_sha256,"content_sha256":self.content_sha256,"postgres_major":17,"locale":self.content.locale,"source":self.content.source,"tables":self.content.tables.iter().map(|t|json!({"table":t.table,"columns":t.columns,"constraints":t.constraints,"rows":t.rows,"bytes":t.copy_text.len(),"data_sha256":t.data_sha256})).collect::<Vec<_>>(),"limits":{"data_bytes":MAX_DATA,"archive_bytes":MAX_ARCHIVE,"tables":MAX_TABLES,"columns_per_table":MAX_COLUMNS,"rows":MAX_ROWS,"row_bytes":MAX_ROW}})
    }
}
pub fn encode(content: Content) -> Result<Vec<u8>> {
    content.validate()?;
    let content_sha256 = hash(&serde_json::to_vec(&content)?);
    let bytes = serde_json::to_vec(&Archive {
        content,
        content_sha256,
    })?;
    if bytes.len() > MAX_ARCHIVE {
        return Err(invalid("logical data archive exceeds 66 MiB"));
    }
    Ok(bytes)
}
pub fn decode(bytes: &[u8]) -> Result<Verified> {
    if bytes.len() > MAX_ARCHIVE {
        return Err(invalid("logical data archive exceeds 66 MiB"));
    }
    let archive: Archive = serde_json::from_slice(bytes)
        .map_err(|_| invalid("invalid logical data archive structure"))?;
    archive.content.validate()?;
    if hash(&serde_json::to_vec(&archive.content)?) != archive.content_sha256 {
        return Err(invalid("logical data content digest differs"));
    }
    Ok(Verified {
        content: archive.content,
        archive_sha256: hash(bytes),
        content_sha256: archive.content_sha256,
    })
}
pub fn read(path: &Path) -> Result<Verified> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.len() > MAX_ARCHIVE as u64 {
        return Err(invalid("logical data requires a bounded regular file"));
    }
    let mut bytes = Vec::new();
    (&file)
        .take(MAX_ARCHIVE as u64 + 1)
        .read_to_end(&mut bytes)?;
    use std::os::unix::fs::MetadataExt;
    let after = file.metadata()?;
    if before.len() != after.len()
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime()
        || before.ctime_nsec() != after.ctime_nsec()
    {
        return Err(conflict("logical data archive changed while reading"));
    }
    decode(&bytes)
}

pub use postgres::{export, import, status};
