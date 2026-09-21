//! Private UC principal broker and live catalog authority. No workload credentials escape.
mod remote;
use crate::{
    authorization::Subject,
    store::{Result, error::invalid},
};
pub(crate) use remote::Broker;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum AdminCommand {
    Status {},
    MapPrincipal {
        principal: String,
    },
    ResolvePrincipal {
        principal: String,
        expected_uc_id: String,
    },
    Plan {
        changes: Vec<Change>,
    },
    Apply {
        plan: String,
        key: String,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Change {
    pub publication: String,
    pub subject: Subject,
    pub publication_revision: i64,
    pub tables: Vec<String>,
    pub present: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum ReadCommand {
    List {
        search: String,
    },
    Describe {
        publication: String,
        table: String,
        publication_revision: i64,
    },
}
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Principal {
    pub principal: String,
    pub subject: String,
    pub uc_id: String,
}
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Object {
    pub kind: String,
    pub name: String,
    pub id: String,
    pub allow_missing: bool,
    pub definition: Option<crate::catalog::publication::Table>,
}
impl Object {
    pub fn key(&self) -> String {
        format!("{}/{}", self.kind, self.name)
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Table {
    pub publication: String,
    pub revision: i64,
    pub deployment: String,
    pub catalog: String,
    pub schema: String,
    pub object: Object,
}
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Origin {
    pub publication: String,
    pub subject: String,
    pub revision: i64,
    pub tables: Vec<String>,
}
// Object -> internal subject -> exact direct privileges. UC still evaluates
// requests itself; this matrix only detects/reconciles administrative drift.
pub(crate) type Grants = BTreeMap<String, BTreeMap<String, BTreeSet<String>>>;
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Snapshot {
    pub revision: i64,
    pub provider: String,
    pub principals: Vec<Principal>,
    pub objects: Vec<Object>,
    pub tables: Vec<Table>,
    pub origins: Vec<Origin>,
    pub desired: Grants,
}
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Plan {
    pub snapshot: Snapshot,
    pub observed: Grants,
}
pub(crate) fn denied() -> crate::store::Error {
    invalid("catalog authority is unavailable or unresolved")
}
pub(crate) fn digest(value: &impl Serialize) -> Result<String> {
    crate::project_apply::digest(value)
}
pub(crate) fn subject(realm: &str, principal: &str) -> Result<String> {
    let realm: uuid::Uuid = realm.parse().map_err(|_| denied())?;
    let principal: uuid::Uuid = principal.parse().map_err(|_| denied())?;
    Ok(format!(
        "p-{}-{}@supabricks.invalid",
        realm.simple(),
        principal.simple()
    ))
}
pub(crate) fn public_table(table: &Table, value: &Value) -> Value {
    serde_json::json!({"publication":table.publication,"publication_revision":table.revision,
        "deployment":table.deployment,"table":table.object.id,"name":value["name"],
        "catalog":value["catalog_name"],"schema":value["schema_name"],"columns":value["columns"]})
}
