//! Governed whole-branch data authority, independent of project control and UC reads.
pub(crate) mod postgres;
use serde::{Deserialize, Serialize};
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Capability {
    Read,
    Write,
    Ddl,
    CopySource,
    Receive,
    Share,
    ManageSync,
    ExecuteSync,
    ReadSync,
}
impl Capability {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::Write => "write",
            Self::Ddl => "ddl",
            Self::CopySource => "copy_source",
            Self::Receive => "receive",
            Self::Share => "share",
            Self::ManageSync => "manage_sync",
            Self::ExecuteSync => "execute_sync",
            Self::ReadSync => "read_sync",
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Find {
        key: String,
    },
    ExportData {
        branch: String,
        selection: crate::projects::data::Selection,
        expected_policy: i64,
        key: String,
    },
    Sql {
        branch: String,
        capability: Capability,
        sql: String,
        expected_policy: i64,
        key: String,
    },
    Import {
        branch: String,
        archive_hex: String,
        expected_policy: i64,
        key: String,
    },
    Clone {
        branch: String,
        name: String,
        expected_policy: i64,
        key: String,
    },
    Export {
        branch: String,
        expected_policy: i64,
        key: String,
    },
    Publish {
        export: String,
        expected_policy: i64,
    },
    Status {
        operation: String,
    },
}
pub(crate) fn denied() -> crate::store::Error {
    crate::store::error::invalid(
        "governed data action is not authorized or its branch profile is unsupported",
    )
}
