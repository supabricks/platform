use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::Command,
};
use supabricks_local::{project::ProjectConfig, recovery, store::Store};
fn digest(path: &Path) -> String {
    hex::encode(Sha256::digest(fs::read(path).unwrap()))
}
fn command(binary: &Path, args: &[&str], root: &Path, success: bool) -> Value {
    let p = Command::new(binary)
        .args(args)
        .arg("--data-dir")
        .arg(root)
        .output()
        .unwrap();
    assert_eq!(
        p.status.success(),
        success,
        "stdout={} stderr={}",
        String::from_utf8_lossy(&p.stdout),
        String::from_utf8_lossy(&p.stderr)
    );
    if success {
        serde_json::from_slice(&p.stdout).unwrap()
    } else {
        json!({"error":String::from_utf8_lossy(&p.stderr)})
    }
}
#[test]
fn stopped_bundle_preserves_metadata_files_and_private_credentials() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("source");
    let project_dir = tmp.path().join("project");
    fs::create_dir(&project_dir).unwrap();
    let project = ProjectConfig::initialize(&project_dir, "example").unwrap();
    let mut store = Store::open(&root).unwrap();
    store.register_project(&project).unwrap();
    drop(store);
    fs::create_dir(root.join("objects")).unwrap();
    fs::write(root.join("objects/payload"), b"durable object").unwrap();
    fs::write(root.join("storage.pk8"), b"private material").unwrap();
    let shm = root.join("computes/00000000-0000-0000-0000-000000000001/pgdata/pg_dynshmem");
    fs::create_dir_all(shm.parent().unwrap()).unwrap();
    symlink("/dev/shm/", &shm).unwrap();
    let backup = tmp.path().join("backup");
    recovery::create(&root, &backup).unwrap();
    let manifest = recovery::verify(&backup).unwrap();
    assert!(!manifest.files.contains_key("owner.lock"));
    let target = tmp.path().join("restored");
    recovery::restore(&backup, &target).unwrap();
    assert_eq!(
        fs::read(target.join("storage.pk8")).unwrap(),
        b"private material"
    );
    assert_eq!(
        fs::metadata(target.join("storage.pk8"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(&target).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert!(
        fs::symlink_metadata(target.join(shm.strip_prefix(&root).unwrap()))
            .unwrap()
            .is_dir()
    );
    let restored = Store::open(&target).unwrap();
    assert_eq!(restored.project(project.id).unwrap().name, "example");
    drop(restored);
    assert!(recovery::restore(&backup, &target).is_err());
    fs::write(backup.join("data/objects/payload"), b"tampered").unwrap();
    assert!(recovery::verify(&backup).is_err());
    let refused = tmp.path().join("refused");
    assert!(recovery::restore(&backup, &refused).is_err());
    assert!(!refused.exists());
}
#[test]
fn rejects_unsafe_sources_paths_and_incomplete_restore_before_state_changes() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("source");
    drop(Store::open(&root).unwrap());
    let before = digest(&root.join("state.sqlite3"));
    assert!(recovery::create(&root, &root.join("nested")).is_err());
    assert_eq!(before, digest(&root.join("state.sqlite3")));
    fs::write(root.join("restore-incomplete"), b"{}").unwrap();
    assert!(Store::open(&root).is_err());
    assert_eq!(before, digest(&root.join("state.sqlite3")));
    fs::remove_file(root.join("restore-incomplete")).unwrap();
    symlink("/etc/passwd", root.join("escape")).unwrap();
    let backup = tmp.path().join("backup");
    assert!(recovery::create(&root, &backup).is_err());
    assert!(!backup.join("backup.json").exists());
    assert!(recovery::verify(&backup).is_err());
}
struct Fixture {
    _tmp: tempfile::TempDir,
    root: PathBuf,
    prefix: PathBuf,
    old: PathBuf,
    new: PathBuf,
    backup: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        Self::with_project(false)
    }
    fn with_project(project: bool) -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        let mut store = Store::open(&root).unwrap();
        if project {
            store
                .register_project(&ProjectConfig {
                    id: supabricks_core::resource::ProjectId::new(),
                    name: "migration-project".into(),
                    format_version: 1,
                })
                .unwrap();
        }
        drop(store);
        let prefix = tmp.path().join("programs");
        fs::create_dir_all(prefix.join("releases")).unwrap();
        fs::create_dir(prefix.join("bin")).unwrap();
        let old = release(&prefix, "v0.1.0-alpha.2");
        let new = release(&prefix, "v0.1.0-alpha.3");
        symlink("releases/v0.1.0-alpha.2", prefix.join("current")).unwrap();
        for name in ["supabricks", "psql"] {
            symlink(
                format!("../current/bin/{name}"),
                prefix.join("bin").join(name),
            )
            .unwrap();
        }
        let cfg = json!({"version":2,"bundle":old.join("engine"),"process_compose":old.join("helpers/process-compose"),"weed":old.join("helpers/weed"),"installation_identity":digest(&old.join("release.json")),"ports":{},"s3_access":"credential-access","s3_secret":"credential-secret","supervisor_token":"supervisor-secret","validation_token":"validation-secret"});
        fs::write(root.join("runtime.json"), serde_json::to_vec(&cfg).unwrap()).unwrap();
        let backup = tmp.path().join("backup");
        Self {
            _tmp: tmp,
            root,
            prefix,
            old,
            new,
            backup,
        }
    }
    fn upgrade(&self, success: bool) -> Value {
        command(
            &self.new.join("bin/supabricks"),
            &[
                "installation",
                "upgrade",
                "--prefix",
                self.prefix.to_str().unwrap(),
                "--previous",
                self.prefix.join("current").to_str().unwrap(),
                "--backup",
                self.backup.to_str().unwrap(),
            ],
            &self.root,
            success,
        )
    }
    fn pending(&self, phase: &str) {
        let completed: Value =
            serde_json::from_slice(&fs::read(self.root.join("last-upgrade.json")).unwrap())
                .unwrap();
        let saved = recovery::verify(&self.backup).unwrap();
        let j = json!({"version":1,"previous":self.old.canonicalize().unwrap(),"prefix":self.prefix.canonicalize().unwrap(),"backup":self.backup.canonicalize().unwrap(),"from":completed["from"],"to":completed["to"],"database_sha256":saved.files["state.sqlite3"].sha256});
        if phase == "prepared" {
            fs::copy(
                self.backup.join("data/runtime.json"),
                self.root.join("runtime.json"),
            )
            .unwrap();
        }
        if phase != "current_activated" {
            fs::remove_file(self.prefix.join("current")).unwrap();
            symlink("releases/v0.1.0-alpha.2", self.prefix.join("current")).unwrap();
        }
        fs::write(
            self.root.join("upgrade.json"),
            serde_json::to_vec(&j).unwrap(),
        )
        .unwrap();
    }
}
fn release(prefix: &Path, version: &str) -> PathBuf {
    let path = prefix.join("releases").join(version);
    for name in ["bin", "engine", "helpers"] {
        fs::create_dir_all(path.join(name)).unwrap();
    }
    fs::copy(
        env!("CARGO_BIN_EXE_supabricks"),
        path.join("bin/supabricks"),
    )
    .unwrap();
    for (name, bytes) in [
        ("engine/manifest.json", b"engine".as_slice()),
        ("helpers/weed", b"same storage"),
        ("bin/psql", b"client"),
    ] {
        fs::write(path.join(name), bytes).unwrap();
    }
    let mut files = BTreeMap::new();
    for name in [
        "bin/supabricks",
        "bin/psql",
        "engine/manifest.json",
        "helpers/weed",
    ] {
        let p = path.join(name);
        files.insert(name,json!({"sha256":digest(&p),"executable":fs::metadata(p).unwrap().permissions().mode()&0o111!=0}));
    }
    let target = if cfg!(target_os = "macos") {
        "macos-arm64"
    } else {
        "linux-x86_64"
    };
    fs::write(path.join("release.json"),serde_json::to_vec(&json!({"format_version":1,"version":version,"profile":"local-postgres-alpha","target":target,"files":files,"provenance":{"data_formats":{"local_catalog":supabricks_local::store::SCHEMA_VERSION,"runtime_config":2,"postgres_major":17,"analytical_snapshot":1}}})).unwrap()).unwrap();
    path
}
#[test]
fn platform_upgrade_backs_up_before_rebinding_and_rejects_downgrade() {
    let f = Fixture::new();
    let original = digest(&f.root.join("runtime.json"));
    let result = f.upgrade(true);
    assert_eq!(result["upgraded"], true);
    assert_eq!(digest(&f.backup.join("data/runtime.json")), original);
    assert!(!f.root.join("upgrade.json").exists());
    assert_eq!(
        fs::read_link(f.prefix.join("current")).unwrap(),
        Path::new("releases/v0.1.0-alpha.3")
    );
    let state = digest(&f.root.join("state.sqlite3"));
    let runtime = digest(&f.root.join("runtime.json"));
    command(
        &f.old.join("bin/supabricks"),
        &[
            "installation",
            "upgrade",
            "--prefix",
            f.prefix.to_str().unwrap(),
            "--previous",
            f.new.to_str().unwrap(),
            "--backup",
            f._tmp.path().join("downgrade").to_str().unwrap(),
        ],
        &f.root,
        false,
    );
    assert_eq!(state, digest(&f.root.join("state.sqlite3")));
    assert_eq!(runtime, digest(&f.root.join("runtime.json")));
    let restored = f._tmp.path().join("old-restored");
    command(
        &f.new.join("bin/supabricks"),
        &[
            "backup",
            "restore",
            f.backup.to_str().unwrap(),
            "--release",
            f.old.to_str().unwrap(),
        ],
        &restored,
        true,
    );
    assert_eq!(original, digest(&restored.join("runtime.json")));
}
#[test]
fn resumes_both_activation_boundaries_and_blocks_startup_during_transaction() {
    let f = Fixture::new();
    f.upgrade(true);
    for phase in ["prepared", "runtime_rebound", "current_activated"] {
        f.pending(phase);
        let before = digest(&f.root.join("state.sqlite3"));
        command(&f.new.join("bin/supabricks"), &["up"], &f.root, false);
        assert_eq!(before, digest(&f.root.join("state.sqlite3")));
        f.upgrade(true);
        assert!(!f.root.join("upgrade.json").exists());
    }
    f.pending("runtime_rebound");
    let retained = f._tmp.path().join("retained-backup");
    fs::rename(&f.backup, &retained).unwrap();
    f.upgrade(false);
    assert!(!f.backup.exists());
    fs::rename(retained, &f.backup).unwrap();
    fs::write(f.backup.join("data/runtime.json"), b"corrupt").unwrap();
    f.upgrade(false);
    assert!(f.root.join("upgrade.json").exists());
    assert_eq!(
        fs::read_link(f.prefix.join("current")).unwrap(),
        Path::new("releases/v0.1.0-alpha.2")
    );
}
#[test]
fn incompatible_components_or_catalog_leave_source_unchanged() {
    let f = Fixture::new();
    let before = digest(&f.root.join("state.sqlite3"));
    let config = digest(&f.root.join("runtime.json"));
    let manifest = f.new.join("release.json");
    let original = fs::read(&manifest).unwrap();
    let mut m: Value = serde_json::from_slice(&original).unwrap();
    fs::write(f.new.join("helpers/weed"), b"different storage").unwrap();
    m["files"]["helpers/weed"]["sha256"] = json!(digest(&f.new.join("helpers/weed")));
    fs::write(&manifest, serde_json::to_vec(&m).unwrap()).unwrap();
    f.upgrade(false);
    assert_eq!(before, digest(&f.root.join("state.sqlite3")));
    assert_eq!(config, digest(&f.root.join("runtime.json")));
    assert!(!f.backup.exists());
    fs::write(f.new.join("helpers/weed"), b"same storage").unwrap();
    fs::write(&manifest, original).unwrap();
    let db = rusqlite::Connection::open(f.root.join("state.sqlite3")).unwrap();
    db.pragma_update(None, "user_version", 999).unwrap();
    drop(db);
    let before = digest(&f.root.join("state.sqlite3"));
    f.upgrade(false);
    assert_eq!(before, digest(&f.root.join("state.sqlite3")));
    assert!(!f.root.join("upgrade.json").exists());
}

#[test]
fn killed_backup_never_publishes_and_releases_ownership() {
    use std::time::{Duration, Instant};
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("data");
    drop(Store::open(&root).unwrap());
    let file = fs::File::create(root.join("large-payload")).unwrap();
    file.set_len(256 * 1024 * 1024).unwrap();
    drop(file);
    let destination = tmp.path().join("interrupted");
    let mut child = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["backup", "create"])
        .arg(&destination)
        .arg("--data-dir")
        .arg(&root)
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !destination.join("data/large-payload").exists() {
        assert!(
            child.try_wait().unwrap().is_none(),
            "backup ended before interruption"
        );
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(1));
    }
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(!destination.join("backup.json").exists());
    assert!(recovery::verify(&destination).is_err());
    assert_eq!(
        fs::metadata(root.join("large-payload")).unwrap().len(),
        256 * 1024 * 1024
    );
    drop(Store::open(&root).unwrap());
}

#[test]
fn catalog_eight_migration_resumes_every_durable_boundary_and_preserves_old_backup() {
    catalog_migration(8);
}
#[test]
fn catalog_nine_migration_resumes_every_durable_boundary_and_preserves_old_backup() {
    catalog_migration(9);
}
#[test]
fn catalog_ten_migration_resumes_every_durable_boundary_and_preserves_old_backup() {
    catalog_migration(10);
}
#[test]
fn catalog_eleven_migration_resumes_every_durable_boundary_and_preserves_old_backup() {
    catalog_migration(11);
}
#[test]
fn catalog_twelve_migration_resumes_every_durable_boundary() {
    catalog_migration(12);
}
#[test]
fn catalog_thirteen_migration_is_additive_and_resumes_durable_boundaries() {
    catalog_migration(13);
}
#[test]
fn catalog_fourteen_migration_preserves_publication_predecessor() {
    catalog_migration(14);
}
#[test]
fn catalog_fifteen_migration_preserves_local_owner_identity() {
    catalog_migration(15);
}
#[test]
fn catalog_sixteen_migration_preserves_identity_and_adds_no_remote_roles() {
    catalog_migration(16);
}
#[test]
fn catalog_seventeen_migration_preserves_roles_and_starts_catalog_access_closed() {
    catalog_migration(17);
    catalog_migration(18);
}
#[test]
fn catalog_nineteen_migration_adds_no_data_grants_or_admitted_branches() {
    catalog_migration(19);
}
fn catalog_migration(source_schema: u32) {
    let f = Fixture::with_project(source_schema >= 17);
    // Construct the predecessor catalog with real migrations 1..8, then use
    // installed-process upgrade handling. The native gate also uses real alpha.3.
    let db = rusqlite::Connection::open(f.root.join("state.sqlite3")).unwrap();
    // Rebuild the legacy parent FKs before removing schema 25's artifact registry.
    for table in ["publications", "analytics_gc"] {
        let sql: String = db
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?1",
                [table],
                |r| r.get(0),
            )
            .unwrap();
        let columns = sql.split_once('(').unwrap().1.replace(
            "REFERENCES analytical_artifacts(id)",
            "REFERENCES exports(id)",
        );
        db.execute_batch(&format!("CREATE TABLE {table}_legacy ({columns}; INSERT INTO {table}_legacy SELECT * FROM {table}; DROP TABLE {table}; ALTER TABLE {table}_legacy RENAME TO {table};")).unwrap();
    }
    db.execute_batch("CREATE UNIQUE INDEX publication_in_flight ON publications(branch_id) WHERE state IN ('requested','files_complete'); DROP TRIGGER export_artifact; DROP TABLE incremental_requests; DROP TABLE incremental_heads; DROP TABLE incremental_runs; DROP TABLE analytical_artifacts;").unwrap();
    if source_schema < 24 {
        db.execute_batch("DROP TABLE capture_requests; DROP TABLE sync_captures;")
            .unwrap();
    }
    if source_schema < 23 {
        db.execute_batch(
            "DROP TABLE sync_requests; DROP TABLE sync_runs; DROP TABLE sync_policies;",
        )
        .unwrap();
    }
    if source_schema < 22 {
        db.execute_batch("DROP TABLE governed_project_requests;")
            .unwrap();
    }
    if source_schema < 21 {
        db.execute_batch("DROP TRIGGER security_identity_audit; DROP TRIGGER security_authorization_audit; DROP TRIGGER security_catalog_grant_audit; DROP TABLE security_audit; DROP TABLE security_state; ALTER TABLE identity_sessions DROP COLUMN authoritative_until_ms;").unwrap();
    }
    if source_schema < 20 {
        db.execute_batch(
            "DROP TABLE data_operations; DROP TABLE data_grants; DROP TABLE governed_branches;",
        )
        .unwrap();
    }
    if source_schema < 18 {
        db.execute_batch("DROP TRIGGER catalog_governance_membership_add; DROP TRIGGER catalog_governance_membership_remove; DROP TRIGGER catalog_governance_disabled; DROP TRIGGER catalog_governance_publication_insert; DROP TRIGGER catalog_governance_publication_update; DROP TABLE catalog_grant_audit; DROP TABLE catalog_grant_plans; DROP TABLE catalog_grant_origins; DROP TABLE catalog_principals; DROP TABLE catalog_governance;").unwrap();
    }
    if source_schema < 19 {
        db.execute_batch("DROP TABLE isolated_executions;").unwrap();
    }
    if source_schema < 17 {
        db.execute_batch("DROP TRIGGER authorization_new_deployment; DROP TRIGGER authorization_membership_added; DROP TRIGGER authorization_membership_removed; DROP TRIGGER authorization_principal_disabled; DROP TABLE authorization_audit; DROP TABLE authorization_mutations; DROP TABLE authorization_executions; DROP TABLE authorization_heads; DROP TABLE authorization_sources; DROP TABLE authorization_grants; DROP TABLE authorization_roles; DROP TABLE authorization_policy;").unwrap();
    }
    if source_schema >= 16 {
        db.execute_batch(r#"
            INSERT INTO identity_principals VALUES ('migration-service','service','Migration service',0);
            INSERT INTO identity_groups VALUES ('migration-group','Migration group');
            INSERT INTO identity_memberships VALUES ('migration-group','migration-service');
            INSERT INTO identity_sessions(token_hash,principal,csrf_hash,channel,scopes,expires_ms,epoch)
                VALUES ('migration-token-hash','migration-service','','service','["identity:self"]',9999999999999,1);
        "#).unwrap();
    }
    if source_schema >= 17 {
        db.execute_batch("INSERT INTO authorization_roles SELECT id,'principal:migration-service','viewer' FROM deployments LIMIT 1; UPDATE authorization_policy SET revision=7;").unwrap();
    }
    if source_schema < 16 {
        db.execute_batch("DROP TABLE identity_audit; DROP TABLE identity_sessions; DROP TABLE identity_logins; DROP TABLE identity_providers; DROP TABLE identity_memberships; DROP TABLE identity_groups; DROP TABLE identity_subjects; DROP TABLE identity_principals; DROP TABLE identity_realm;").unwrap();
    }
    if source_schema < 15 {
        db.execute_batch("DROP TABLE catalog_publication_refs; DROP TABLE catalog_retention; DROP TABLE catalog_heads; DROP TABLE catalog_publications;").unwrap();
    }
    if source_schema < 14 {
        db.execute_batch("DROP TABLE catalog_assets; DROP TABLE catalog_namespaces;")
            .unwrap();
    }
    if source_schema < 12 {
        db.execute_batch("DROP TABLE deployment_active; DROP TABLE deployment_revisions; DROP TABLE deployment_resources; DROP TABLE project_applies;").unwrap();
    }
    if source_schema < 11 {
        db.execute_batch("DROP TABLE binding_operations; DROP TABLE worktree_bindings; DROP TABLE deployments; DROP TABLE project_definitions; DROP TABLE principals; DROP TABLE workspaces; DROP TABLE realms;").unwrap();
    }
    if source_schema < 10 {
        db.execute_batch("DROP TABLE environment_leases; DROP TABLE environment_active; DROP TABLE environment_operations; DROP TABLE environment_generations;").unwrap();
    }
    if source_schema == 8 {
        db.execute_batch("DROP TABLE ingest_jobs; DROP TABLE ingest_sources; DROP TABLE ingest_identity; DROP TABLE catalog_migrations;").unwrap();
    }
    let predecessor_identity = if source_schema >= 11 {
        Some(
            db.query_row(
                "SELECT r.id,p.id FROM realms r JOIN principals p ON p.realm_id=r.id",
                [],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
            )
            .unwrap(),
        )
    } else {
        None
    };
    db.pragma_update(None, "user_version", source_schema)
        .unwrap();
    drop(db);
    let manifest = f.old.join("release.json");
    let mut old: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    old["provenance"]["data_formats"]["local_catalog"] = json!(source_schema);
    fs::write(&manifest, serde_json::to_vec(&old).unwrap()).unwrap();
    let mut runtime: Value =
        serde_json::from_slice(&fs::read(f.root.join("runtime.json")).unwrap()).unwrap();
    runtime["installation_identity"] = json!(digest(&manifest));
    fs::write(
        f.root.join("runtime.json"),
        serde_json::to_vec(&runtime).unwrap(),
    )
    .unwrap();
    let before = digest(&f.root.join("state.sqlite3"));
    // Ordinary startup cannot silently migrate, even with no runtime binding.
    fs::rename(f.root.join("runtime.json"), f.root.join("runtime.saved")).unwrap();
    assert!(Store::open(&f.root).is_err());
    assert_eq!(before, digest(&f.root.join("state.sqlite3")));
    fs::rename(f.root.join("runtime.saved"), f.root.join("runtime.json")).unwrap();
    f.upgrade(true);
    let saved = recovery::verify(&f.backup).unwrap();
    assert_eq!(saved.schema_version, source_schema);
    let backup_hash = digest(&f.backup.join("data/state.sqlite3"));
    for phase in [
        "before_migration",
        "after_migration",
        "runtime_rebound",
        "current_activated",
    ] {
        f.pending(
            if phase == "before_migration" || phase == "after_migration" {
                "prepared"
            } else {
                phase
            },
        );
        if phase == "before_migration" {
            fs::copy(
                f.backup.join("data/state.sqlite3"),
                f.root.join("state.sqlite3"),
            )
            .unwrap();
        }
        command(&f.new.join("bin/supabricks"), &["up"], &f.root, false);
        f.upgrade(true);
        assert_eq!(backup_hash, digest(&f.backup.join("data/state.sqlite3")));
        let db = rusqlite::Connection::open(f.root.join("state.sqlite3")).unwrap();
        if let Some(expected) = &predecessor_identity {
            let actual: (String, String) = db
                .query_row("SELECT id,local_owner FROM identity_realm", [], |r| {
                    Ok((r.get(0)?, r.get(1)?))
                })
                .unwrap();
            assert_eq!(&actual, expected);
        }
        assert_eq!(
            db.pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))
                .unwrap(),
            supabricks_local::store::SCHEMA_VERSION
        );
        assert_eq!(
            db.query_row("SELECT count(*) FROM authorization_roles", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            if source_schema >= 17 { 1 } else { 0 }
        );
        assert_eq!(
            db.query_row("SELECT count(*) FROM authorization_grants", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(db.query_row("SELECT count(*) FROM deployments d LEFT JOIN authorization_policy p ON p.deployment=d.id WHERE p.revision IS NULL OR p.revision!=?1", [if source_schema >= 17 {7} else {1}], |r| r.get::<_, i64>(0)).unwrap(), 0);
        assert_eq!(
            db.query_row("SELECT state FROM catalog_governance", [], |r| r
                .get::<_, String>(0))
                .unwrap(),
            "dirty"
        );
        assert_eq!(
            db.query_row("SELECT count(*) FROM catalog_grant_origins", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(db.query_row("SELECT (SELECT count(*) FROM data_grants)+(SELECT count(*) FROM governed_branches)+(SELECT count(*) FROM data_operations)",[],|r|r.get::<_,i64>(0)).unwrap(),0);
        if source_schema >= 16 {
            let preserved: (String, String, i64) = db.query_row(
                "SELECT s.principal,s.scopes,s.expires_ms FROM identity_sessions s JOIN identity_memberships m ON m.principal=s.principal WHERE s.token_hash='migration-token-hash' AND m.group_id='migration-group'",
                [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            ).unwrap();
            assert_eq!(
                preserved,
                (
                    "migration-service".into(),
                    "[\"identity:self\"]".into(),
                    9999999999999
                )
            );
        }

        assert_eq!(
            db.query_row(
                "SELECT source_sha256 FROM catalog_migrations WHERE version=25",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            backup_hash
        );
    }
    let rollback = f._tmp.path().join("catalog-eight-restored");
    command(
        &f.new.join("bin/supabricks"),
        &[
            "backup",
            "restore",
            f.backup.to_str().unwrap(),
            "--release",
            f.old.to_str().unwrap(),
        ],
        &rollback,
        true,
    );
    if source_schema < 21 {
        assert_eq!(backup_hash, digest(&rollback.join("state.sqlite3")));
    } else {
        let restored = rusqlite::Connection::open(rollback.join("state.sqlite3")).unwrap();
        assert!(
            restored
                .query_row("SELECT restore_closed FROM security_state", [], |r| r
                    .get::<_, bool>(0))
                .unwrap()
        );
        assert_eq!(
            restored
                .query_row("SELECT count(*) FROM identity_sessions", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
    assert_eq!(
        rusqlite::Connection::open(rollback.join("state.sqlite3"))
            .unwrap()
            .pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))
            .unwrap(),
        source_schema
    );
}

#[test]
fn first_catalog_upgrade_preserves_engine_fences_and_requires_no_catalog_state() {
    for case in [
        "add",
        "engine_changed",
        "existing_catalog",
        "unsupported_schema",
    ] {
        let f = Fixture::new();
        let file = f.new.join("share/unity-catalog/build.json");
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(&file, b"new catalog closure").unwrap();
        let path = f.new.join("release.json");
        let mut manifest: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        manifest["files"]["share/unity-catalog/build.json"] =
            json!({"sha256":digest(&file),"executable":false});
        manifest["provenance"]["data_formats"]["unity_catalog"] = json!(1);
        if case == "engine_changed" {
            let engine = f.new.join("engine/manifest.json");
            fs::write(&engine, b"different PG engine").unwrap();
            manifest["files"]["engine/manifest.json"]["sha256"] = json!(digest(&engine));
        }
        fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        if case == "existing_catalog" {
            fs::create_dir(f.root.join("catalog")).unwrap();
        }
        if case == "unsupported_schema" {
            let path = f.old.join("release.json");
            let mut manifest: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            manifest["provenance"]["data_formats"]["local_catalog"] = json!(7);
            fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
            let runtime = f.root.join("runtime.json");
            let mut cfg: Value = serde_json::from_slice(&fs::read(&runtime).unwrap()).unwrap();
            cfg["installation_identity"] = json!(digest(&path));
            fs::write(runtime, serde_json::to_vec(&cfg).unwrap()).unwrap();
            let db = rusqlite::Connection::open(f.root.join("state.sqlite3")).unwrap();
            db.pragma_update(None, "user_version", 7).unwrap();
        }
        let before = digest(&f.root.join("runtime.json"));
        f.upgrade(case == "add");
        if case == "add" {
            assert_eq!(digest(&f.backup.join("data/runtime.json")), before);
            for phase in ["prepared", "runtime_rebound", "current_activated"] {
                f.pending(phase);
                f.upgrade(true);
            }
        } else {
            assert_eq!(digest(&f.root.join("runtime.json")), before);
            assert!(!f.backup.exists());
            assert!(!f.root.join("upgrade.json").exists());
        }
    }
}

#[test]
fn catalog_build_timings_do_not_change_upgrade_payload_compatibility() {
    for changed_payload in [false, true] {
        let f = Fixture::new();
        for (release, duration) in [(&f.old, 1), (&f.new, 2)] {
            let report = release.join("share/unity-catalog/build.json");
            let jar = release.join("share/unity-catalog/jars/server.jar");
            fs::create_dir_all(jar.parent().unwrap()).unwrap();
            fs::write(&report, format!("{{\"build_seconds\":{duration}}}")).unwrap();
            fs::write(
                &jar,
                if changed_payload && release == &f.new {
                    b"changed backend".as_slice()
                } else {
                    b"same backend".as_slice()
                },
            )
            .unwrap();
            let path = release.join("release.json");
            let mut manifest: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            for file in [&report, &jar] {
                manifest["files"][file.strip_prefix(release).unwrap().to_str().unwrap()] =
                    json!({"sha256":digest(file),"executable":false});
            }
            manifest["provenance"]["data_formats"]["unity_catalog"] = json!(1);
            fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        }
        let path = f.root.join("runtime.json");
        let mut runtime: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        runtime["installation_identity"] = json!(digest(&f.old.join("release.json")));
        fs::write(path, serde_json::to_vec(&runtime).unwrap()).unwrap();
        f.upgrade(!changed_payload);
        if !changed_payload {
            let backup = recovery::verify(&f.backup).unwrap();
            let completed: Value =
                serde_json::from_slice(&fs::read(f.root.join("last-upgrade.json")).unwrap())
                    .unwrap();
            // Distinct full fingerprints survive in the persisted journal and
            // backup, even though the payload-only upgrade comparison passed.
            assert_ne!(
                completed["from"]["compatibility"],
                completed["to"]["compatibility"]
            );
            assert_eq!(
                backup.release.unwrap().compatibility,
                completed["from"]["compatibility"]
            );
            command(
                &f.new.join("bin/supabricks"),
                &[
                    "backup",
                    "restore",
                    f.backup.to_str().unwrap(),
                    "--release",
                    f.old.to_str().unwrap(),
                ],
                &f._tmp.path().join("restored"),
                true,
            );
        } else {
            assert!(!f.backup.exists());
        }
    }
}

#[test]
fn schema_twenty_upgrade_preserves_governed_data() {
    catalog_migration(20);
}

#[test]
fn governed_backup_rollback_cannot_resurrect_sessions_principals_or_grants() {
    use supabricks_local::identity::{AdminCommand as A, AuthCommand, Channel};
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("source");
    let mut store = Store::open(&root).unwrap();
    let id = store
        .identity_admin(A::Service {
            label: "historical".into(),
        })
        .unwrap()["principal_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let token = store
        .identity_admin(A::IssueService {
            principal: id.clone(),
            scopes: vec!["identity:self".into()],
            ttl_seconds: 3600,
        })
        .unwrap()["token"]
        .as_str()
        .unwrap()
        .to_owned();
    let realm = store.identity_admin(A::Status).unwrap()["realm_id"]
        .as_str()
        .unwrap()
        .to_owned();
    drop(store);
    let backup = tmp.path().join("backup");
    recovery::create(&root, &backup).unwrap();
    let mut store = Store::open(&root).unwrap();
    store
        .identity_admin(A::Disable {
            principal: id.clone(),
            disabled: true,
        })
        .unwrap();
    drop(store);
    let target = tmp.path().join("restored");
    let restored = recovery::restore(&backup, &target).unwrap();
    assert_eq!(restored["governed_closed"], true);
    assert_eq!(restored["credentials_restored"], false);
    let mut store = Store::open(&target).unwrap();
    let status = store.identity_admin(A::RestoreStatus).unwrap();
    let restore_id = status["restore_id"].as_str().unwrap().to_owned();
    assert_eq!(status["closed"], true);
    assert!(
        store
            .identity_auth(AuthCommand::Authenticate {
                token: token.clone(),
                channel: Channel::Service,
                csrf: None
            })
            .is_err()
    );
    assert!(
        store
            .identity_admin(A::IssueService {
                principal: id.clone(),
                scopes: vec!["identity:self".into()],
                ttl_seconds: 300
            })
            .is_err()
    );
    assert!(
        store
            .identity_admin(A::RestoreReconcile {
                restore_id: restore_id.clone(),
                realm_id: "foreign-realm".into()
            })
            .is_err()
    );
    store
        .identity_admin(A::RestoreReconcile {
            restore_id,
            realm_id: realm,
        })
        .unwrap();
    assert!(
        store
            .identity_auth(AuthCommand::Authenticate {
                token,
                channel: Channel::Service,
                csrf: None
            })
            .is_err()
    );
    assert!(
        store
            .identity_admin(A::IssueService {
                principal: id.clone(),
                scopes: vec!["identity:self".into()],
                ttl_seconds: 300
            })
            .is_err()
    );
    store
        .identity_admin(A::Disable {
            principal: id.clone(),
            disabled: false,
        })
        .unwrap();
    let fresh = store
        .identity_admin(A::IssueService {
            principal: id,
            scopes: vec!["identity:self".into()],
            ttl_seconds: 300,
        })
        .unwrap()["token"]
        .as_str()
        .unwrap()
        .to_owned();
    assert!(
        store
            .identity_auth(AuthCommand::Authenticate {
                token: fresh,
                channel: Channel::Service,
                csrf: None
            })
            .is_ok()
    );
}

#[test]
fn schema_twenty_one_upgrade_preserves_security_and_adds_console_requests() {
    catalog_migration(21);
}

#[test]
fn schema_twenty_two_upgrade_preserves_existing_state_and_adds_snapshot_policies() {
    catalog_migration(22);
}

#[test]
fn catalog_twenty_three_migration_adds_capture_without_source_resources() {
    catalog_migration(23);
}

#[test]
fn schema_twenty_four_upgrade_preserves_capture_and_adds_incremental_publications() {
    catalog_migration(24);
}
