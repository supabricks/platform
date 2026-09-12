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
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("data");
        drop(Store::open(&root).unwrap());
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
    fs::write(path.join("release.json"),serde_json::to_vec(&json!({"format_version":1,"version":version,"profile":"local-postgres-alpha","target":target,"files":files,"provenance":{"data_formats":{"local_catalog":10,"runtime_config":2,"postgres_major":17,"analytical_snapshot":1}}})).unwrap()).unwrap();
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
fn catalog_migration(source_schema: u32) {
    let f = Fixture::new();
    // Construct the exact pre-I00 catalog with real migrations 1..8, then use
    // installed-process upgrade handling. The native gate also uses real alpha.3.
    let db = rusqlite::Connection::open(f.root.join("state.sqlite3")).unwrap();
    db.execute_batch("DROP TABLE environment_leases; DROP TABLE environment_active; DROP TABLE environment_operations; DROP TABLE environment_generations;").unwrap();
    if source_schema == 8 {
        db.execute_batch("DROP TABLE ingest_jobs; DROP TABLE ingest_sources; DROP TABLE ingest_identity; DROP TABLE catalog_migrations;").unwrap();
    }
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
        assert_eq!(
            db.pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))
                .unwrap(),
            10
        );
        assert_eq!(
            db.query_row(
                "SELECT source_sha256 FROM catalog_migrations WHERE version=10",
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
    assert_eq!(backup_hash, digest(&rollback.join("state.sqlite3")));
    assert_eq!(
        rusqlite::Connection::open(rollback.join("state.sqlite3"))
            .unwrap()
            .pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))
            .unwrap(),
        source_schema
    );
}
