//! Stopped H2 recovery for the pinned local-owner backend. Never run against a live service.
use super::{Provider, config, publication::Publication, runtime};
use crate::{
    recovery::{atomic_json, sync_dir},
    store::{
        Result,
        error::{conflict, invalid},
    },
};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    fs,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

pub const MAX_METADATA_BYTES: u64 = 256 * 1024 * 1024;
pub const MAX_PUBLICATIONS: u64 = 1024;
pub const MAX_RETAINED_BYTES: u64 = 64 * 1024 * 1024 * 1024;
const MAX_SCRIPT: usize = 32 * 1024 * 1024;

pub fn contract() -> Value {
    let pin: Value = serde_json::from_str(include_str!(
        "../../../../components/unity-catalog-source.lock.json"
    ))
    .expect("pinned source");
    json!({"format_version":1,"backend":"h2-2.2.224","backend_schema":1,
        "server_commit":pin["commit"],"publication_manifest":1,"capability_profile":"local-owner-files-v1",
        "migration":"exact-backend-only; incompatible changes require a separately qualified migration"})
}
// UC01–UC06 used this exact backend before the independent sentinel existed.
// Adopt only that pinned contract; an existing different contract is never rewritten.
pub fn ensure_contract(root: &Path) -> Result<()> {
    let path = root.join("catalog-format.json");
    if path.try_exists()? {
        if serde_json::from_slice::<Value>(&config::private_bytes(&path, 16384)?)? != contract() {
            return Err(conflict(
                "catalog backend contract differs; restore with its source release or use a qualified migration",
            ));
        }
    } else {
        atomic_json(&path, &contract())?;
    }
    Ok(())
}
fn provider(root: &Path) -> Result<Option<config::Config>> {
    let path = root.join("catalog-provider.json");
    if !path.try_exists()? {
        if root.join("catalog-local.json").try_exists()? || root.join("catalog").try_exists()? {
            return Err(conflict(
                "catalog provider configuration is missing; repair it before recovery",
            ));
        }
        return Ok(None);
    }
    let value: config::Config = serde_json::from_slice(&config::private_bytes(&path, 16384)?)?;
    if value.version != 1
        || value
            .provider_id
            .parse::<supabricks_core::resource::ProjectId>()
            .is_err()
    {
        return Err(conflict(
            "catalog provider configuration version or identity is invalid",
        ));
    }
    Ok(Some(value))
}
fn quoted(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}
fn assertion(script: &mut String, condition: &str) {
    // Dynamic division prevents an optimizer from folding the failing branch.
    script.push_str(&format!(
        "CALL 1 / CASE WHEN ({condition}) THEN 1 ELSE 0 END;\n"
    ));
}
fn string(v: &Value) -> Result<&str> {
    v.as_str()
        .ok_or_else(|| invalid("catalog recovery descriptor is incomplete"))
}
fn bounded_records(db: &Connection) -> Result<Vec<Publication>> {
    let mut q = db.prepare(
        "SELECT record_json FROM catalog_publications WHERE state!='retired' ORDER BY id LIMIT 129",
    )?;
    let rows = q
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if rows.len() > 128 {
        return Err(conflict("catalog recovery publication limit exceeded"));
    }
    rows.into_iter()
        .map(|s| {
            if s.len() > 2 * 1024 * 1024 {
                return Err(invalid("catalog publication exceeds recovery bound"));
            }
            Ok(serde_json::from_str(&s)?)
        })
        .collect()
}
fn schema(db: &Connection) -> Result<u32> {
    Ok(db.pragma_query_value(None, "user_version", |r| r.get(0))?)
}

pub fn usage(root: &Path, db: &Connection) -> Result<Value> {
    let metadata = root
        .join("catalog/etc/db/h2db.mv.db")
        .metadata()
        .map(|m| m.len())
        .unwrap_or(0);
    let count: i64 = db.query_row("SELECT count(*) FROM catalog_publications", [], |r| {
        r.get(0)
    })?;
    let namespaces: i64 =
        db.query_row("SELECT count(*) FROM catalog_namespaces", [], |r| r.get(0))?;
    let assets: i64 = db.query_row("SELECT count(*) FROM catalog_assets", [], |r| r.get(0))?;
    let retained: i64 = db.query_row("SELECT COALESCE(sum(CAST(json_extract(f.value,'$.bytes') AS INTEGER)*CASE WHEN json_extract(p.descriptor,'$.format_version')=2 THEN 2 ELSE 1 END),0) FROM publications p, json_each(p.descriptor,'$.manifest.files') f WHERE p.epoch_id IN (SELECT epoch_id FROM catalog_retention)",[],|r|r.get(0))?;
    Ok(
        json!({"namespaces":namespaces,"observed_assets":assets,"metadata_bytes":metadata,"publication_records":count,"retained_snapshot_bytes":retained,
        "limits":{"metadata_bytes":MAX_METADATA_BYTES,"publication_records":MAX_PUBLICATIONS,"active_publications":128,"tables_per_publication":128,"retained_snapshot_bytes":MAX_RETAINED_BYTES,"namespaces":128,"observed_assets":8192,"offline_script_bytes":MAX_SCRIPT,"offline_timeout_seconds":60,"jvm_heap_mib":256}}),
    )
}
fn run_h2(root: &Path, provider: &Provider, script: &str, writable: bool) -> Result<()> {
    if script.len() > MAX_SCRIPT {
        return Err(conflict(
            "catalog reconciliation exceeds its bounded script budget",
        ));
    }
    let runtime = runtime::resolve(provider)?;
    let file = root.join("catalog/recovery.sql");
    crate::supervisor::write_private(&file, script.as_bytes())?;
    // Relative JDBC path prevents the filesystem path from being interpreted as JDBC options.
    let result = (|| -> Result<()> {
        let mut child = Command::new(runtime.root.join("java/bin/java"))
            .arg(format!(
                "-Djdk.net.hosts.file={}",
                root.join("catalog/etc/conf/hosts").display()
            ))
            .args([
                "-Xms32m",
                "-Xmx256m",
                "-XX:ActiveProcessorCount=2",
                "-cp",
                &runtime.classpath,
                "org.h2.tools.RunScript",
                "-url",
                if writable {
                    "jdbc:h2:file:./etc/db/h2db;IFEXISTS=TRUE;LOCK_TIMEOUT=1000"
                } else {
                    "jdbc:h2:file:./etc/db/h2db;IFEXISTS=TRUE;LOCK_TIMEOUT=1000;ACCESS_MODE_DATA=r"
                },
                "-user",
                "",
                "-password",
                "",
                "-script",
                "recovery.sql",
            ])
            .current_dir(root.join("catalog"))
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("LC_ALL", runtime::java_locale())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(60);
        loop {
            if let Some(status) = child.try_wait()? {
                return if status.success() {
                    Ok(())
                } else {
                    Err(conflict(
                        "catalog checkpoint failed identity, schema or backend integrity checks; preserve this root and restore a verified backup",
                    ))
                };
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(conflict(
                    "catalog checkpoint deadline exceeded; preserve the stopped root and inspect recovery",
                ));
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    })();
    let _ = fs::remove_file(file);
    result
}

/// Locks/processes/readers have already drained under Stopped. A partial
/// publication or retained session cannot become an apparently complete backup.
pub fn checkpoint(root: &Path, db: &Connection) -> Result<()> {
    if schema(db)? < 14 {
        return Ok(());
    }
    if root.join("catalog-format.json").try_exists()? {
        ensure_contract(root)?;
    }
    let Some(cfg) = provider(root)? else {
        return Ok(());
    };
    ensure_contract(root)?;
    if schema(db)? >= 15 {
        if db.prepare("SELECT 1 FROM catalog_publications WHERE state IN ('registering','retiring') UNION ALL SELECT 1 FROM catalog_publication_refs WHERE reference_key NOT LIKE 'binding:%' UNION ALL SELECT 1 FROM project_applies WHERE state IN ('queued','preparing','activating')")?.exists([])? {
            return Err(conflict("catalog publications, applies or readers need reconciliation; resume or retire pending work before backup"));
        }
        let u = usage(root, db)?;
        if u["metadata_bytes"].as_u64().unwrap() > MAX_METADATA_BYTES
            || u["publication_records"].as_u64().unwrap() > MAX_PUBLICATIONS
            || u["retained_snapshot_bytes"].as_u64().unwrap() > MAX_RETAINED_BYTES
        {
            return Err(conflict(
                "catalog recovery resource limit exceeded; release unused publications or use an operator recovery export",
            ));
        }
    }
    reconcile(root, root, db, &cfg, false)
}

pub fn restore(root: &Path, source: &Path, db: &Connection) -> Result<()> {
    if schema(db)? < 14 {
        return Ok(());
    }
    let Some(cfg) = provider(root)? else {
        return Ok(());
    };
    ensure_contract(root)?;
    reconcile(root, source, db, &cfg, true)?;
    if matches!(cfg.provider, Provider::External { .. }) {
        atomic_json(
            &root.join("catalog-external-restore.json"),
            &json!({"version":1,"state":"explicit_rebind_required","remote_metadata_included":false}),
        )?;
    }
    if root.join("catalog/etc/db/h2db.mv.db").try_exists()? {
        // The existing restartable key-rotation protocol creates fresh keys and tokens.
        atomic_json(&root.join("catalog/rotate-key.json"), &json!({"version":1}))?;
    }
    Ok(())
}
fn reconcile(
    root: &Path,
    source: &Path,
    db: &Connection,
    cfg: &config::Config,
    restore: bool,
) -> Result<()> {
    if schema(db)? >= 15 {
        validate_references(db)?;
    }
    let identity_path = root.join("catalog-local.json");
    if !identity_path.try_exists()? {
        if root.join("catalog/etc/db/h2db.mv.db").try_exists()? {
            return Err(conflict(
                "catalog identity is missing; recovery cannot adopt an untracked metastore",
            ));
        }
        return Ok(());
    }
    let identity: config::LocalIdentity =
        serde_json::from_slice(&config::private_bytes(&identity_path, 4096)?)?;
    if identity.version != 1
        || (matches!(cfg.provider, Provider::Local { .. })
            && cfg.provider_id != identity.provider_id)
    {
        return Err(conflict(
            "catalog provider differs from owned metastore identity",
        ));
    }
    let Some(metastore) = identity.metastore_id else {
        if root.join("catalog/etc/db/h2db.mv.db").try_exists()? {
            return Err(conflict(
                "catalog bootstrap is incomplete; complete owned startup before checkpointing",
            ));
        }
        return Ok(());
    };
    let h2 = root.join("catalog/etc/db/h2db.mv.db");
    let meta = fs::symlink_metadata(&h2)?;
    if !meta.is_file() || meta.len() > MAX_METADATA_BYTES {
        return Err(conflict(
            "catalog backend is missing or exceeds recovery bounds",
        ));
    }
    let owned_provider = match &cfg.provider {
        Provider::Local { .. } => cfg.provider.clone(),
        Provider::External { .. } => Provider::Local { runtime: None },
    };
    let mut script = String::from("SET AUTOCOMMIT FALSE;\n");
    assertion(
        &mut script,
        &format!(
            "(SELECT COUNT(*) FROM uc_metastore)=1 AND (SELECT COUNT(*) FROM uc_metastore WHERE id={})=1",
            quoted(&metastore)
        ),
    );
    let mut changed = vec![];
    if schema(db)? >= 15 {
        for mut p in bounded_records(db)? {
            if p.state != "published"
                || p.error.is_some()
                || p.namespace.provider_id != identity.provider_id
                || p.namespace.metastore_id != metastore
            {
                return Err(conflict(
                    "catalog publication requires explicit reconciliation before recovery",
                ));
            }
            let (export,descriptor,state):(String,String,String)=db.query_row("SELECT p.export_id,p.descriptor,s.state FROM publications p JOIN snapshots s ON s.epoch_id=p.epoch_id WHERE p.epoch_id=?1 AND p.branch_id=?2",params![p.epoch_id.to_string(),p.branch_id.to_string()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
            let d: Value = serde_json::from_str(&descriptor)?;
            let generation = root.join("analytics/generations").join(&export);
            if state != "available"
                || d["manifest_sha256"] != p.manifest_hash
                || export
                    .parse::<supabricks_core::resource::OperationId>()
                    .is_err()
            {
                return Err(conflict("catalog snapshot identity changed"));
            }
            crate::analytics::check_ready(&generation, &d)?;
            let is_view = d["format_version"] == 2;
            let (generation, d) = if is_view {
                let view = crate::epoch_view::View::plan(root, &d)?;
                view.verify()?;
                (view.root, view.descriptor)
            } else {
                (generation, d)
            };
            // Verify every immutable file, not just its length, before changing a location.
            for f in d["manifest"]["files"]
                .as_array()
                .ok_or_else(|| invalid("snapshot file inventory missing"))?
            {
                if crate::recovery::file_hash(&generation.join(string(&f["path"])?))?
                    != string(&f["sha256"])?
                {
                    return Err(conflict(
                        "retained snapshot content differs from publication",
                    ));
                }
            }
            let tables = d["manifest"]["tables"]
                .as_array()
                .ok_or_else(|| invalid("snapshot tables missing"))?;
            if tables.len() != p.tables.len() {
                return Err(conflict("catalog snapshot table set changed"));
            }
            let ns = &p.namespace;
            let catalog = ns
                .catalog_id
                .as_ref()
                .ok_or_else(|| conflict("catalog UUID missing"))?;
            let schema = ns
                .schema_id
                .as_ref()
                .ok_or_else(|| conflict("schema UUID missing"))?;
            assertion(
                &mut script,
                &format!(
                    "(SELECT COUNT(*) FROM uc_catalogs WHERE id={} AND name={})=1",
                    quoted(catalog),
                    quoted(&ns.catalog)
                ),
            );
            assertion(
                &mut script,
                &format!(
                    "(SELECT COUNT(*) FROM uc_schemas WHERE id={} AND name={} AND catalog_id={})=1",
                    quoted(schema),
                    quoted(&ns.schema),
                    quoted(catalog)
                ),
            );
            for (table, t) in tables.iter().zip(&mut p.tables) {
                let oid = table["oid"]
                    .as_u64()
                    .ok_or_else(|| invalid("snapshot table OID missing"))?
                    .to_string();
                let mut relative = Path::new("analytics/generations").join(&export);
                if is_view {
                    relative = relative.join("shared");
                }
                let relative = relative.join(&oid);
                let old = super::publication::location_uri(&source.join(&relative))?;
                let new = super::publication::local_location(&root.join(&relative))?;
                if t.body["storage_location"] != old
                    || table["path"] != oid
                    || table["version"] != 0
                    || table["schema"] != t.source_schema
                    || table["name"] != t.source_name
                {
                    return Err(conflict("catalog owned mapping changed"));
                }
                let columns =
                    super::publication::columns(&generation, &generation.join(&oid), &d, table)?;
                if t.body["columns"] != json!(columns) {
                    return Err(conflict("catalog column schema changed"));
                }
                let id = quoted(&t.id.to_string());
                assertion(
                    &mut script,
                    &format!(
                        "(SELECT COUNT(*) FROM uc_tables WHERE id={id} AND schema_id={} AND name={} AND url={} AND data_source_format='DELTA' AND type='EXTERNAL' AND column_count={})=1",
                        quoted(schema),
                        quoted(string(&t.body["name"])?),
                        quoted(&old),
                        columns.len()
                    ),
                );
                assertion(
                    &mut script,
                    &format!("(SELECT COUNT(*) FROM sb_publication_identities WHERE id={id})=1"),
                );
                assertion(
                    &mut script,
                    &format!(
                        "(SELECT COUNT(*) FROM uc_columns WHERE table_id={id})={}",
                        columns.len()
                    ),
                );
                for c in &columns {
                    assertion(
                        &mut script,
                        &format!(
                            "(SELECT COUNT(*) FROM uc_columns WHERE table_id={id} AND name={} AND ordinal_position={} AND type_text={} AND type_json={} AND type_name={} AND nullable={})=1",
                            quoted(string(&c["name"])?),
                            c["position"],
                            quoted(string(&c["type_text"])?),
                            quoted(string(&c["type_json"])?),
                            quoted(string(&c["type_name"])?),
                            c["nullable"]
                        ),
                    );
                }
                if restore {
                    script.push_str(&format!(
                        "UPDATE uc_tables SET url={} WHERE id={id};\n",
                        quoted(&new)
                    ));
                    t.body["storage_location"] = json!(new);
                }
            }
            if restore {
                changed.push(p);
            }
        }
    }
    script.push_str("COMMIT;\nSHUTDOWN;\n");
    run_h2(root, &owned_provider, &script, restore)?;
    if restore {
        // restore-incomplete guards both databases across these separate commits.
        // On interruption the intact backup is restored into a fresh destination.
        let tx = db.unchecked_transaction()?;
        for p in changed {
            tx.execute(
                "UPDATE catalog_publications SET record_json=?2 WHERE id=?1",
                params![p.id.to_string(), serde_json::to_string(&p)?],
            )?;
        }
        tx.commit()?;
        atomic_json(
            &root.join("catalog-restore.json"),
            &json!({"version":1,"source_root":source,"data_root":root,"metastore_id":metastore,"state":"reconciled","credentials":"rotate_before_start"}),
        )?;
        sync_dir(&root.join("catalog/etc/db"))?;
    }
    Ok(())
}

/// Bind upgrade retries to the stopped backend and credentials as well as SQLite.
pub fn checkpoint_identity(root: &Path) -> Result<Option<String>> {
    let names = [
        "catalog-provider.json",
        "catalog-local.json",
        "catalog-format.json",
        "catalog/etc/db/h2db.mv.db",
        "catalog/etc/conf/private_key.der",
        "catalog/etc/conf/public_key.der",
        "catalog/etc/conf/key_id.txt",
        "catalog/etc/conf/token.txt",
    ];
    let mut entries = std::collections::BTreeMap::new();
    for name in names {
        let path = root.join(name);
        if path.try_exists()? {
            entries.insert(name, crate::recovery::file_hash(&path)?);
        }
    }
    Ok(if entries.is_empty() {
        None
    } else {
        Some(crate::recovery::hash(&serde_json::to_vec(&entries)?))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn backend_contract_is_independent_and_never_silently_migrated() {
        let root = tempfile::tempdir().unwrap();
        ensure_contract(root.path()).unwrap();
        let before = checkpoint_identity(root.path()).unwrap();
        ensure_contract(root.path()).unwrap();
        assert_eq!(checkpoint_identity(root.path()).unwrap(), before);
        let mut other = contract();
        other["backend_schema"] = json!(999);
        atomic_json(&root.path().join("catalog-format.json"), &other).unwrap();
        assert!(ensure_contract(root.path()).is_err());
        assert_ne!(checkpoint_identity(root.path()).unwrap(), before);
        assert_eq!(
            serde_json::from_slice::<Value>(
                &fs::read(root.path().join("catalog-format.json")).unwrap()
            )
            .unwrap(),
            other
        );
    }
    #[test]
    fn external_restore_remains_blocked_until_explicit_reconfiguration() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = crate::store::Store::open(&dir.path().join("data")).unwrap();
        atomic_json(
            &store.root().join("catalog-external-restore.json"),
            &json!({"version":1}),
        )
        .unwrap();
        let mut manager = super::super::Manager::recover(&mut store);
        assert_eq!(
            manager.status()["error"],
            "external_restore_requires_explicit_rebind"
        );
        assert!(
            manager
                .command(&mut store, super::super::Command::Restart)
                .is_err()
        );
        assert!(!manager.status().to_string().contains("token"));
    }
    #[test]
    fn unbootstrapped_catalog_has_no_backend_to_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let store = crate::store::Store::open(&dir.path().join("data")).unwrap();
        let cfg = config::Config {
            version: 1,
            provider_id: "00000000-0000-4000-8000-000000000001".into(),
            provider: Provider::Local { runtime: None },
        };
        atomic_json(&store.root().join("catalog-provider.json"), &cfg).unwrap();
        // A session reference without its binding/publication is caught by SQLite
        // foreign key verification in Stopped; publication work is checked here.
        assert!(
            checkpoint(
                store.root(),
                &Connection::open(store.root().join("state.sqlite3")).unwrap()
            )
            .is_ok()
        );
    }
}

fn validate_references(db: &Connection) -> Result<()> {
    use std::collections::{BTreeMap, BTreeSet};
    let publications: BTreeMap<_, _> = bounded_records(db)?
        .into_iter()
        .map(|p| (p.id.to_string(), p))
        .collect();
    let rows=db.prepare("SELECT deployment_id,logical,record_json FROM deployment_resources WHERE json_extract(record_json,'$.kind')='catalog_dataset' LIMIT 513")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
    if rows.len() > 512 {
        return Err(conflict("catalog binding recovery limit exceeded"));
    }
    let mut expected = BTreeSet::new();
    for (owner, logical, record) in rows {
        let resource: crate::project_apply::Resource = serde_json::from_str(&record)?;
        let d = super::datasets::from_resource(&resource)?;
        let p = publications
            .get(&d.target.publication_id.to_string())
            .ok_or_else(|| conflict("dataset binding lost its publication"))?;
        if p.namespace.provider_id != d.target.provider_id
            || p.deployment_id != d.target.deployment_id
            || p.epoch_id != d.epoch_id
            || p.revision != Some(d.revision)
            || p.manifest_hash != d.content_sha256
        {
            return Err(conflict(
                "dataset binding identity differs from publication journal",
            ));
        }
        expected.insert((p.id.to_string(), format!("binding:{owner}:{logical}")));
    }
    let actual=db.prepare("SELECT publication_id,reference_key FROM catalog_publication_refs WHERE reference_key LIKE 'binding:%'")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<rusqlite::Result<BTreeSet<_>>>()?;
    if actual != expected {
        return Err(conflict(
            "dataset binding retention differs from installed resources",
        ));
    }
    for p in publications.values() {
        let count: i64 = db.query_row(
            "SELECT count(*) FROM catalog_retention WHERE publication_id=?1 AND epoch_id=?2",
            params![p.id.to_string(), p.epoch_id.to_string()],
            |r| r.get(0),
        )?;
        if count != 1 {
            return Err(conflict(
                "catalog publication has lost its snapshot retention",
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod retention_tests {
    use super::*;
    #[test]
    fn incomplete_retention_and_unowned_binding_references_fail_closed() {
        let (_dir, mut store, _, mut p) = super::super::publication::tests::setup();
        store.begin_catalog_publication(&p).unwrap();
        for t in &mut p.tables {
            t.state = "verified".into();
        }
        store.commit_catalog_publication(&mut p).unwrap();
        let db = Connection::open(store.root().join("state.sqlite3")).unwrap();
        validate_references(&db).unwrap();
        db.execute(
            "INSERT INTO catalog_publication_refs VALUES (?1,'binding:foreign:dataset.sales')",
            [p.id.to_string()],
        )
        .unwrap();
        assert!(validate_references(&db).is_err());
        db.execute("DELETE FROM catalog_publication_refs", [])
            .unwrap();
        db.execute("DELETE FROM catalog_retention", []).unwrap();
        assert!(validate_references(&db).is_err());
    }
}
