//! PK01 source schema. Deserialization never binds a runtime project.
use crate::{
    project::ProjectConfig,
    store::{Result, error::invalid},
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use supabricks_core::resource::ProjectId;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub format_version: u32,
    pub id: ProjectId,
    pub name: String,
    pub package: Package,
    #[serde(default)]
    pub requires: Requirements,
    #[serde(default)]
    pub targets: BTreeMap<String, Target>,
    #[serde(default)]
    pub resources: BTreeMap<String, BTreeMap<String, Resource>>,
    #[serde(default)]
    pub environments: BTreeMap<String, Environment>,
    /// Explicit fragments; relative to the project root, never relative to a fragment.
    #[serde(default)]
    pub include: Vec<String>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Package {
    pub version: String,
    pub include: Vec<String>,
    pub notebook_outputs: OutputPolicy,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputPolicy {
    Strip,
}
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Requirements {
    #[serde(default)]
    pub capabilities: Vec<String>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub mode: Mode,
    #[serde(default)]
    pub default: bool,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Development,
    Production,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Environment {
    pub pyproject: String,
    pub lock: String,
    /// NE04 exports, explicitly qualified for each native target.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub bundles: BTreeMap<String, String>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Resource {
    CatalogDataset {
        requirement: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        expected_schema_sha256: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        expected_content_sha256: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        provenance: Option<crate::catalog::datasets::Target>,
        #[serde(default)]
        depends_on: Vec<String>,
    },
    Migration {
        file: String,
        database: String,
        sequence: u32,
        #[serde(default)]
        depends_on: Vec<String>,
    },
    Fixture {
        file: String,
        database: String,
        schema: String,
        table: String,
        mapping: crate::ingest::Mapping,
        #[serde(default)]
        depends_on: Vec<String>,
    },
    PostgresDatabase {
        lifecycle: Lifecycle,
        #[serde(default)]
        depends_on: Vec<String>,
    },
    Sql {
        engine: Engine,
        file: String,
        database: String,
        #[serde(default)]
        depends_on: Vec<String>,
    },
    Notebook {
        file: String,
        environment: String,
        database: String,
        #[serde(default)]
        depends_on: Vec<String>,
    },
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Lifecycle {
    Retain,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Engine {
    Postgres,
    Spark,
}
impl Resource {
    pub fn group(&self) -> &str {
        match self {
            Self::PostgresDatabase { .. } => "database",
            Self::CatalogDataset { .. } => "dataset",
            Self::Sql { .. } => "query",
            Self::Migration { .. } => "migration",
            Self::Fixture { .. } => "fixture",
            Self::Notebook { .. } => "notebook",
        }
    }
    pub fn dependencies(&self) -> Vec<String> {
        let (depends, database) = match self {
            Self::PostgresDatabase { depends_on, .. } | Self::CatalogDataset { depends_on, .. } => {
                (depends_on, None)
            }
            Self::Migration {
                depends_on,
                database,
                ..
            }
            | Self::Fixture {
                depends_on,
                database,
                ..
            }
            | Self::Sql {
                depends_on,
                database,
                ..
            }
            | Self::Notebook {
                depends_on,
                database,
                ..
            } => (depends_on, Some(database)),
        };
        let mut result = depends.clone();
        if let Some(database) = database {
            result.push(database.clone());
        }
        result.sort();
        result.dedup();
        result
    }
}
#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fragment {
    #[serde(default)]
    pub resources: BTreeMap<String, BTreeMap<String, Resource>>,
    #[serde(default)]
    pub include: Vec<String>,
}

pub fn parse<T: serde::de::DeserializeOwned>(bytes: &[u8], path: &str) -> Result<T> {
    let source =
        std::str::from_utf8(bytes).map_err(|_| invalid(format!("{path}: expected UTF-8")))?;
    toml::from_str(source).map_err(|_| {
        invalid(format!(
            "{path}: invalid TOML or unsupported fields/types; consult the PK01 schema"
        ))
    })
}
pub fn version(bytes: &[u8]) -> Result<u32> {
    let v: toml::Value = parse(bytes, "supabricks.toml")?;
    match v.get("format_version").and_then(toml::Value::as_integer) {
        Some(1) => Ok(1),
        Some(2) => Ok(2),
        _ => Err(invalid(
            "unsupported project format_version; expected 1 or 2",
        )),
    }
}
pub fn identity(id: ProjectId, name: &str) -> Result<ProjectConfig> {
    let config = ProjectConfig {
        format_version: 1,
        id,
        name: name.into(),
    };
    config.validate()?;
    Ok(config)
}
pub fn key(name: &str) -> Result<()> {
    if name.is_empty()
        || name.len() > 63
        || !name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
    {
        return Err(invalid(
            "resource, environment and target keys must use 1–63 lowercase ASCII letters, digits, _ or -",
        ));
    }
    Ok(())
}
