//! Explicit, restartable platform upgrades. Engine changes require export/restore.
use crate::{
    installation::Installation,
    recovery::{self, Release, Stopped},
    store::{
        Result, SCHEMA_VERSION,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Journal {
    version: u32,
    previous: PathBuf,
    prefix: PathBuf,
    backup: PathBuf,
    from: Release,
    to: Release,
    database_sha256: String,
}
fn installed(path: &Path) -> Result<Installation> {
    Installation::at_executable(&path.join("bin/supabricks").canonicalize()?)?
        .ok_or_else(|| invalid("missing installed release"))
}
fn version(v: &str) -> Result<semver::Version> {
    semver::Version::parse(v.strip_prefix('v').unwrap_or(v))
        .map_err(|_| invalid("invalid release version"))
}
fn compatible(from: &Release, to: &Release) -> Result<()> {
    if version(&to.version)? <= version(&from.version)? {
        return Err(conflict(
            "upgrades must increase the release version; downgrades require restoring a backup with its source release",
        ));
    }
    if from.target != to.target
        || from.profile != to.profile
        || from.compatibility != to.compatibility
    {
        return Err(conflict(
            "incompatible engine, library, analytical or data format; this release supports platform-only upgrades with identical component inventories",
        ));
    }
    Ok(())
}
fn read_runtime(root: &Path) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(
        root.join("runtime.json"),
    )?)?)
}
fn same_runtime(value: &Value, release: &Release) -> bool {
    value["installation_identity"].as_str() == Some(&release.identity)
}
fn prefix_check(prefix: &Path, candidate: &Installation) -> Result<PathBuf> {
    let prefix = prefix.canonicalize()?;
    if candidate.root != prefix.join("releases").join(&candidate.manifest.version) {
        return Err(invalid(
            "upgrade must run from the verified staged release inside this installation",
        ));
    }
    for name in ["supabricks", "psql"] {
        if fs::read_link(prefix.join("bin").join(name))? != Path::new("../current/bin").join(name) {
            return Err(conflict(
                "installation command links are not installer-owned",
            ));
        }
    }
    Ok(prefix)
}
pub(crate) fn run(root: &Path, prefix: &Path, previous: &Path, backup: &Path) -> Result<Value> {
    let candidate = Installation::discover()?
        .ok_or_else(|| invalid("upgrade requires an installed candidate"))?;
    candidate.verify()?;
    let to = Release::of(&candidate)?;
    let prefix = prefix_check(prefix, &candidate)?;
    let _installation_lock = lock(&prefix)?;
    let root = root.canonicalize()?;
    let backup = backup
        .parent()
        .ok_or_else(|| invalid("backup path needs a parent"))?
        .canonicalize()?
        .join(
            backup
                .file_name()
                .ok_or_else(|| invalid("backup path needs a name"))?,
        );
    if root.starts_with(&prefix)
        || prefix.starts_with(&root)
        || backup.starts_with(&root)
        || backup.starts_with(&prefix)
    {
        return Err(invalid(
            "program, data and backup directories must be separate",
        ));
    }
    let journal_path = root.join("upgrade.json");
    let existing: Option<Journal> = if journal_path.exists() {
        Some(serde_json::from_slice(&fs::read(&journal_path)?)?)
    } else {
        None
    };
    let previous = existing
        .as_ref()
        .map(|j| j.previous.clone())
        .unwrap_or(previous.canonicalize()?);
    let old = installed(&previous)?;
    old.verify()?;
    let from = Release::of(&old)?;
    let source_formats = recovery::formats(&old);
    let target_formats = recovery::formats(&candidate);
    let source_schema = source_formats["local_catalog"]
        .as_u64()
        .and_then(|n| u32::try_from(n).ok())
        .ok_or_else(|| conflict("invalid source catalog format"))?;
    if target_formats
        != json!({"local_catalog":SCHEMA_VERSION,"runtime_config":2,"postgres_major":17,"analytical_snapshot":1})
    {
        return Err(conflict(
            "candidate format declaration does not match this binary",
        ));
    }
    let migration = matches!(source_schema, 8 | 9);
    let mut normalized = source_formats.clone();
    normalized["local_catalog"] = json!(SCHEMA_VERSION);
    if migration && normalized == target_formats {
        // Compare the full original inventory with ONLY the named catalog format changed.
        compatible(&Release::with_formats(&old, normalized)?, &to)?;
    } else {
        compatible(&from, &to)?;
    }
    if old.root != prefix.join("releases").join(&from.version) {
        return Err(invalid("previous release is outside this installation"));
    }
    let link = fs::read_link(prefix.join("current"))?;
    if link != Path::new("releases").join(&from.version)
        && link != Path::new("releases").join(&to.version)
    {
        return Err(conflict("current release changed outside the upgrade"));
    }
    if let Some(j) = &existing {
        if j.version != 1 || j.prefix != prefix || j.from != from || j.to != to {
            return Err(conflict(
                "pending upgrade belongs to a different release or installation",
            ));
        }
    }
    let cfg = read_runtime(&root)?;
    if cfg["version"] != 2
        || (!same_runtime(&cfg, &from) && !(existing.is_some() && same_runtime(&cfg, &to)))
    {
        return Err(conflict(
            "data root does not belong to the expected release/runtime format",
        ));
    }
    // Read-only catalog preflight precedes shutdown and all upgrade mutations.
    let db = rusqlite::Connection::open_with_flags(
        root.join("state.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    let schema: u32 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
    if (schema != source_schema && !(existing.is_some() && schema == SCHEMA_VERSION))
        || db
            .prepare("SELECT 1 FROM endpoints WHERE pg_major!=17")?
            .exists([])?
    {
        return Err(conflict(
            "unsupported stored schema or PostgreSQL catalog; upgrade rejected before changing data",
        ));
    }
    drop(db);
    if crate::client::request(&root, crate::daemon::Request::Status).is_ok() {
        if existing.is_some() && same_runtime(&cfg, &to) {
            return Err(conflict(
                "stop the upgraded runtime before completing its pending transaction",
            ));
        }
        let output = Command::new(old.root.join("bin/supabricks"))
            .args(["down", "--data-dir"])
            .arg(&root)
            .output()?;
        if !output.status.success() {
            return Err(conflict(
                "previous release could not stop; run its down command and inspect its private diagnostics",
            ));
        }
    }
    // The lock also excludes a daemon starting between shutdown and acquisition.
    let mut stopped = Stopped::open_schema(&root, schema)?;
    let mut cfg = read_runtime(&root)?;
    let current_hash = recovery::file_hash(&root.join("state.sqlite3"))?;
    let journal = if let Some(mut j) = existing {
        if j.backup != backup && same_runtime(&cfg, &from) && schema == source_schema {
            // An explicit new backup path can refresh a prepared transaction if
            // the old release was used after an interrupted preflight.
            if backup.exists() {
                return Err(conflict("choose a new backup directory"));
            }
            j.backup = backup.clone();
            j.database_sha256 = current_hash.clone();
            recovery::atomic_json(&journal_path, &j)?;
        }
        if j.backup != backup || (schema == source_schema && j.database_sha256 != current_hash) {
            return Err(conflict(
                "data changed since upgrade preparation; retry with a new backup path while still on the old release",
            ));
        }
        j
    } else {
        if backup.exists() {
            return Err(conflict("upgrade backup destination already exists"));
        }
        let j = Journal {
            version: 1,
            previous: old.root.clone(),
            prefix: prefix.clone(),
            backup: backup.clone(),
            from: from.clone(),
            to: to.clone(),
            database_sha256: current_hash,
        };
        recovery::atomic_json(&journal_path, &j)?;
        j
    };
    if !backup.exists() {
        if !same_runtime(&cfg, &from) || schema != source_schema {
            return Err(conflict(
                "pre-upgrade backup is missing after rebinding; recover that bundle before completing this transaction",
            ));
        }
        recovery::create_locked(&stopped, &backup, Some(from.clone()))?;
    }
    let saved = recovery::verify(&backup)?;
    if saved.source_root != root
        || saved.release.as_ref() != Some(&from)
        || saved.files["state.sqlite3"].sha256 != journal.database_sha256
    {
        return Err(conflict(
            "upgrade backup does not match the stopped source state",
        ));
    }
    if !same_runtime(&cfg, &from) && !same_runtime(&cfg, &to) {
        return Err(conflict("runtime changed during upgrade"));
    }
    if migration {
        if schema == source_schema {
            crate::store::migrations::catalog_upgrade(
                &mut stopped.db,
                source_schema,
                &journal.database_sha256,
                &to.identity,
            )?;
        } else {
            let marker: (String, String) = stopped.db.query_row(
                "SELECT source_sha256,release_identity FROM catalog_migrations WHERE version=?1",
                [SCHEMA_VERSION],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            if marker != (journal.database_sha256.clone(), to.identity.clone()) {
                return Err(conflict(
                    "catalog migration does not match the verified upgrade journal",
                ));
            }
        }
        let busy: i64 = stopped
            .db
            .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| r.get(0))?;
        if busy != 0 {
            return Err(conflict("migration checkpoint is busy; retry upgrade"));
        }
        recovery::sync_dir(&root)?;
    }
    cfg["installation_identity"] = json!(to.identity);
    cfg["bundle"] = json!(candidate.bundle());
    cfg["process_compose"] = json!(candidate.helpers().join("process-compose"));
    cfg["weed"] = json!(candidate.helpers().join("weed"));
    // One metadata replace, then one current-link replace. A durable journal
    // blocks new binaries from opening either half of an interrupted update.
    recovery::atomic_json(&root.join("runtime.json"), &cfg)?;
    let tmp = prefix.join(format!(
        ".current-{}",
        supabricks_core::resource::OperationId::new()
    ));
    symlink(Path::new("releases").join(&to.version), &tmp)?;
    fs::rename(&tmp, prefix.join("current"))?;
    recovery::sync_dir(&prefix)?;
    recovery::atomic_json(
        &root.join("last-upgrade.json"),
        &json!({"from":from,"to":to,"backup_id":saved.id,"backup":backup}),
    )?;
    fs::remove_file(journal_path)?;
    recovery::sync_dir(&root)?;
    Ok(
        json!({"upgraded":true,"version":to.version,"backup":backup,"backup_id":saved.id,"runtime":"stopped","data_retained":true}),
    )
}
pub(crate) fn uninstall(root: &Path) -> Result<Value> {
    let current = Installation::discover()?
        .ok_or_else(|| invalid("uninstall requires an installed release"))?;
    current.verify()?;
    let prefix = current
        .root
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| invalid("invalid release layout"))?;
    prefix_check(prefix, &current)?;
    let _installation_lock = lock(prefix)?;
    if fs::read_link(prefix.join("current"))?
        != Path::new("releases").join(&current.manifest.version)
    {
        return Err(conflict("uninstall must run from the current release"));
    }
    crate::runtime_cli::shutdown(root)?;
    let _stopped = if root.exists() {
        Some(Stopped::open(root)?)
    } else {
        None
    };
    // Keep immutable versions for recovery and any other explicitly configured
    // data roots. Remove only the exact installer-owned entry points.
    for name in ["supabricks", "psql"] {
        fs::remove_file(prefix.join("bin").join(name))?;
    }
    fs::remove_file(prefix.join("current"))?;
    recovery::sync_dir(&prefix.join("bin"))?;
    recovery::sync_dir(prefix)?;
    Ok(
        json!({"uninstalled":true,"data_retained":true,"release_files_retained":true,"releases":prefix.join("releases")}),
    )
}

fn lock(prefix: &Path) -> Result<std::fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(prefix.join("upgrade.lock"))?;
    file.try_lock()
        .map_err(|_| conflict("another upgrade or uninstall owns this installation"))?;
    Ok(file)
}
