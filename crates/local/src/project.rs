//! Public project identity only. Credentials, selections and absolute paths live in SQLite.
use crate::store::error::{Result, conflict, invalid};
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::Path,
};
use supabricks_core::{
    resource::{OperationId, ProjectId},
    validation::valid_name,
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProjectConfig {
    pub format_version: u32,
    pub id: ProjectId,
    pub name: String,
}
impl ProjectConfig {
    pub fn validate(&self) -> Result<()> {
        if self.format_version != 1 {
            return Err(invalid("unsupported project format version"));
        }
        let canonical = valid_name(&serde_json::json!({"name": self.name}), "name")
            .map_err(supabricks_core::error::OperationError::from)?;
        if canonical != self.name {
            return Err(invalid("project name must be canonical lowercase"));
        }
        Ok(())
    }
    pub fn read(directory: &Path) -> Result<Self> {
        let text = fs::read_to_string(directory.join("supabricks.toml"))?;
        let value: toml::Value = toml::from_str(&text)?;
        if value
            .get("format_version")
            .and_then(toml::Value::as_integer)
            == Some(2)
        {
            return Err(invalid(
                "format-2 source definitions require deployment binding; use project create, attach or adopt instead of legacy initialization",
            ));
        }
        let config: Self = toml::from_str(&text)?;
        config.validate()?;
        Ok(config)
    }
    /// Publish without overwriting an existing file. A retry recovers its stable ID.
    pub fn initialize(directory: &Path, name: &str) -> Result<Self> {
        let directory = directory.canonicalize()?;
        let canonical = valid_name(&serde_json::json!({"name": name}), "name")
            .map_err(supabricks_core::error::OperationError::from)?;
        let config = Self {
            format_version: 1,
            id: ProjectId::new(),
            name: canonical,
        };
        let target = directory.join("supabricks.toml");
        let temp = directory.join(format!(".supabricks-{}.tmp", OperationId::new()));
        let publish = || -> Result<()> {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temp)?;
            file.write_all(toml::to_string_pretty(&config)?.as_bytes())?;
            file.sync_all()?;
            fs::hard_link(&temp, &target)?;
            File::open(&directory)?.sync_all()?;
            Ok(())
        };
        let result = publish();
        let _ = fs::remove_file(&temp);
        match result {
            Ok(()) => Ok(config),
            Err(crate::store::Error::Io(e)) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                let existing = Self::read(&directory)?;
                if existing.name != config.name {
                    return Err(conflict(
                        "project file already exists with a different name",
                    ));
                }
                Ok(existing)
            }
            Err(e) => Err(e),
        }
    }
}
