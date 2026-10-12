//! Release-local PostgreSQL extensions. Only the operator API can call this module.
//! Names select reviewed SQL; callers never supply SQL, paths, versions or CASCADE.
use crate::{
    api::Binding,
    console::workspace::Target,
    engine::Cell,
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::Duration;
use tokio_postgres::{Config, NoTls};

pub const MANAGED: &[&str] = &[
    "pg_stat_statements",
    "pgcrypto",
    "pg_trgm",
    "uuid-ossp",
    "citext",
];
const REQUIRED: &[&str] = &["neon", "neon_rmgr", "neon_utils", "plpgsql"];
const SCHEMA: &str = "supabricks_extensions";
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    List {
        target: Target,
    },
    Set {
        target: Target,
        name: String,
        enabled: bool,
    },
}
impl Command {
    fn target(&self) -> &Target {
        match self {
            Self::List { target } | Self::Set { target, .. } => target,
        }
    }
    fn validate(&self) -> Result<()> {
        if let Self::Set { name, .. } = self {
            if !MANAGED.contains(&name.as_str()) {
                return Err(invalid(
                    "extension is required or not in the managed release catalogue",
                ));
            }
        }
        Ok(())
    }
}
fn pg(e: tokio_postgres::Error) -> crate::store::Error {
    conflict(format!(
        "PostgreSQL extension request failed ({})",
        e.code()
            .map(|c| c.code())
            .unwrap_or("connection; outcome may be uncertain, refresh the catalogue")
    ))
}
/// Short bounded control transaction, serialized with branch lifecycle by the daemon.
/// Suspended computes are never awakened. Governed branches cannot use owner ingress.
pub fn handle(
    store: &Store,
    cell: Option<&Cell>,
    binding: &Binding,
    command: Command,
) -> Result<Value> {
    command.validate()?;
    let target = command.target();
    target.validate(store, binding)?;
    if store.governed_branch(target.branch)? {
        return Err(invalid(
            "extension management is not yet qualified for governed branches",
        ));
    }
    let branch = store.branch(target.branch)?;
    let cell = cell.ok_or_else(|| conflict("engine is disabled"))?;
    if branch.endpoint.desired_state != supabricks_core::resource::DesiredState::Running
        || branch.revision != branch.observed_revision
        || !cell.connection_ready(store, &branch)?
    {
        return Err(conflict(
            "extension catalogue requires a running, ready compute; resume it explicitly",
        ));
    }
    let capture_active = store.capture_profile(target.branch)?.is_some();
    if matches!(command, Command::Set { .. }) && capture_active {
        return Err(conflict(
            "extension changes require removing the active capture first",
        ));
    }
    let port = branch
        .ports
        .ok_or_else(|| conflict("compute ports unavailable"))?
        .sql;
    let password = store.endpoint_password(branch.endpoint.id)?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    runtime.block_on(async {
        tokio::time::timeout(Duration::from_secs(4), async {
            let (mut client, connection) = Config::new().host("127.0.0.1").port(port)
                .user("cloud_admin").password(password).dbname("postgres")
                .application_name("supabricks-extensions")
                .options("-c statement_timeout=2000 -c lock_timeout=500 -c search_path=pg_catalog -c log_statement=none -c log_min_error_statement=panic")
                .connect(NoTls).await.map_err(pg)?;
            let connection = Connection(tokio::spawn(connection));
            let tx = client.transaction().await.map_err(pg)?;
            // A caller-owned event trigger must not run inside privileged DDL.
            let triggers: bool = tx.query_one("SELECT EXISTS(SELECT 1 FROM pg_event_trigger WHERE evtenabled <> 'D')", &[]).await.map_err(pg)?.get(0);
            if let Command::Set {name, enabled, ..} = &command {
                if triggers { return Err(conflict("extension changes are refused while event triggers are enabled")); }
                tx.query_one("SELECT pg_advisory_xact_lock(253, 1)", &[]).await.map_err(pg)?;
                let existing = tx.query_opt("SELECT e.extowner = 'cloud_admin'::regrole AND n.nspname=$2 AND n.nspowner='cloud_admin'::regrole FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname=$1", &[name, &SCHEMA]).await.map_err(pg)?;
                if existing.as_ref().is_some_and(|r| !r.get::<_, bool>(0)) {
                    return Err(conflict("existing extension is outside the managed schema/owner; automatic adoption is refused"));
                }
                if *enabled && existing.is_none() {
                    if tx.query_opt("SELECT 1 FROM pg_available_extensions WHERE name=$1", &[name]).await.map_err(pg)?.is_none() {
                        return Err(conflict("extension is not shipped by this installed engine; no download attempted"));
                    }
                    if name == "pg_stat_statements" {
                        let loaded: bool = tx.query_one("SELECT 'pg_stat_statements'=ANY(string_to_array(replace(current_setting('shared_preload_libraries'),' ',''),','))", &[]).await.map_err(pg)?.get(0);
                        if !loaded { return Err(conflict("pg_stat_statements requires a qualified engine and compute restart")); }
                    }
                    let schema = tx.query_opt("SELECT nspowner='cloud_admin'::regrole AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(nspacl,acldefault('n',nspowner))) a WHERE a.privilege_type='CREATE' AND a.grantee <> nspowner) FROM pg_namespace WHERE nspname=$1", &[&SCHEMA]).await.map_err(pg)?;
                    if schema.as_ref().is_some_and(|r| !r.get::<_, bool>(0)) { return Err(conflict("managed extension schema has unsafe ownership or grants")); }
                    tx.batch_execute("CREATE SCHEMA IF NOT EXISTS supabricks_extensions AUTHORIZATION cloud_admin; REVOKE CREATE ON SCHEMA supabricks_extensions FROM PUBLIC; GRANT USAGE ON SCHEMA supabricks_extensions TO PUBLIC").await.map_err(pg)?;
                    // Identifier comes from MANAGED, not arbitrary user text. RESTRICT
                    // is intentional: neither installation nor removal adds dependencies.
                    tx.batch_execute(&format!("CREATE EXTENSION \"{name}\" SCHEMA supabricks_extensions")).await.map_err(pg)?;
                } else if !*enabled && existing.is_some() {
                    tx.batch_execute(&format!("DROP EXTENSION \"{name}\" RESTRICT")).await.map_err(pg)?;
                }
            }
            let rows = tx.query(r#"SELECT coalesce(a.name,e.extname)::text, a.default_version, e.extversion, left(a.comment,1024),
                coalesce(e.extowner='cloud_admin'::regrole AND n.nspname='supabricks_extensions' AND n.nspowner='cloud_admin'::regrole,false),
                ARRAY(SELECT v.version FROM pg_available_extension_versions v WHERE v.name=a.name ORDER BY v.version),
                ARRAY(SELECT t.typname::text FROM pg_type t JOIN pg_depend d ON d.classid='pg_type'::regclass AND d.objid=t.oid AND d.refclassid='pg_extension'::regclass AND d.refobjid=e.oid AND d.deptype='e' WHERE t.typtype IN ('b','c','d','e','r','m') ORDER BY t.typname), n.nspname::text
                FROM pg_available_extensions a FULL JOIN pg_extension e ON a.name=e.extname
                LEFT JOIN pg_namespace n ON n.oid=e.extnamespace ORDER BY coalesce(a.name,e.extname) LIMIT 129"#, &[]).await.map_err(pg)?;
            if rows.len()>128 { return Err(conflict("extension catalogue exceeds 128 entries")); }
            let preload: String=tx.query_one("SHOW shared_preload_libraries", &[]).await.map_err(pg)?.get(0);
            let mut extensions=Vec::new();
            for row in rows {
                // Missing control files leave enabled extensions visible but unshipped.
                let name: String=row.get(0);
                let enabled: Option<String>=row.get(2);
                let managed=MANAGED.contains(&name.as_str());
                let shipped = row.get::<_,Option<String>>(1).is_some();
                let owned:bool=row.get(4);
                let ready=name!="pg_stat_statements" || preload.split(',').any(|x| x.trim()==name);
                let types:Vec<String>=row.get(6);
                extensions.push(json!({"name":name,"available_version":row.get::<_,Option<String>>(1),"installed_version":enabled,"enabled":enabled.is_some(),"description":row.get::<_,Option<String>>(3),"available_versions":row.get::<_,Vec<String>>(5),"shipped":shipped,"required":REQUIRED.contains(&name.as_str()),"managed":managed,"can_enable":managed && shipped && ready && !capture_active && !triggers && enabled.is_none(),"can_disable":managed && owned && !capture_active && !triggers && enabled.is_some(),"requires_restart":!ready,"schema":row.get::<_,Option<String>>(7),"analytical_sync":{"policy":"supported_builtin_column_types_only","extension_types":types.iter().map(|t|json!({"name":t,"supported":false})).collect::<Vec<_>>(),"note":sync_note(&name)}}));
            }
            for name in MANAGED.iter().copied().chain(["vector", "postgis"]) {
                if !extensions.iter().any(|e| e["name"] == name) {
                    extensions.push(json!({"name":name,"available_version":null,"installed_version":null,"enabled":false,"available_versions":[],"shipped":false,"required":false,"managed":MANAGED.contains(&name),"can_enable":false,"can_disable":false,"requires_restart":false,"schema":null,"analytical_sync":{"policy":"unqualified","extension_types":[],"note":sync_note(name)}}));
                }
            }
            tx.commit().await.map_err(pg)?;
            drop(connection);
            Ok(json!({"branch_id":target.branch,"revision":target.revision,"extensions":extensions,"downloads":false,"governed_supported":false,"capture_active":capture_active,"event_triggers_active":triggers}))
        }).await.map_err(|_| conflict("extension request timed out; refresh the catalogue before retrying"))?
    })
}
fn sync_note(name: &str) -> &'static str {
    match name {
        "citext" => {
            "citext columns are unsupported; case-insensitive semantics are not mapped to text"
        }
        "uuid-ossp" => "UUID result columns are currently unsupported by analytical sync",
        "pgcrypto" => {
            "bytea result columns are currently unsupported; supported text columns remain eligible"
        }
        _ => {
            "extension functions and indexes are not replicated; eligibility is checked for every stored column"
        }
    }
}
struct Connection(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for Connection {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_reviewed_identifiers_can_reach_ddl() {
        let target = Target {
            branch: Default::default(),
            revision: 1,
        };
        for name in [
            "neon",
            "plpgsql",
            "vector",
            "postgis",
            "pgcrypto\" CASCADE;--",
            "PGCRYPTO",
            "",
        ] {
            assert!(
                Command::Set {
                    target: target.clone(),
                    name: name.into(),
                    enabled: true
                }
                .validate()
                .is_err()
            );
        }
        for name in MANAGED {
            assert!(
                Command::Set {
                    target: target.clone(),
                    name: (*name).into(),
                    enabled: false
                }
                .validate()
                .is_ok()
            );
        }
        assert!(serde_json::from_value::<Command>(json!({"action":"set","target":target,"name":"citext","enabled":false,"cascade":true})).is_err());
    }
}
