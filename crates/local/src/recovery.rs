//! Coordinated stopped-cell recovery. The manifest is the publication boundary.
//! Bundles contain credentials and are private directories, not live-root copies.
use crate::{
    installation::Installation,
    store::{
        Result, SCHEMA_VERSION,
        error::{conflict, invalid},
        ownership::DataRoot,
    },
};
use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Release {
    pub identity: String,
    pub version: String,
    pub target: String,
    pub profile: String,
    pub compatibility: String,
}
impl Release {
    pub(crate) fn of(i: &Installation) -> Result<Self> {
        Self::with_formats(i, formats(i))
    }
    pub(crate) fn with_formats(i: &Installation, formats: Value) -> Result<Self> {
        // Exact engine/library inventory includes PG catalog and extension code;
        // worker lock binds Delta/Arrow/Python dependencies. No guessed engine
        // format compatibility, cross-target restore or major-version upgrade.
        let files: BTreeMap<_, _> = i
            .manifest
            .files
            .iter()
            .filter(|(name, _)| {
                name.starts_with("engine/")
                    || *name == "helpers/weed"
                    || *name == "python/analytics/uv.lock"
                    || *name == "provenance/analytical-runtime.lock.json"
            })
            .map(|(name, f)| (name, (&f.sha256, f.executable)))
            .collect();
        if !files.contains_key(&"engine/manifest.json".to_string())
            || !files.contains_key(&"helpers/weed".to_string())
        {
            return Err(invalid(
                "release has no engine/storage compatibility inventory",
            ));
        }
        Ok(Self {
            identity: i.identity.clone(),
            version: i.manifest.version.clone(),
            target: i.manifest.target.clone(),
            profile: i.manifest.profile.clone(),
            compatibility: hash(&serde_json::to_vec(
                &json!({"files":files,"formats":formats}),
            )?),
        })
    }
}
pub(crate) fn formats(i: &Installation) -> Value {
    i.manifest.provenance.get("data_formats").cloned().unwrap_or_else(||
        json!({"local_catalog":8,"runtime_config":2,"postgres_major":17,"analytical_snapshot":1}))
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub bytes: u64,
    pub sha256: String,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub format_version: u32,
    pub id: String,
    pub consistency: String,
    pub source_root: PathBuf,
    pub schema_version: u32,
    pub release: Option<Release>,
    pub directories: Vec<String>,
    pub files: BTreeMap<String, Entry>,
}
pub(crate) fn hash(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
pub(crate) fn private_dir(path: &Path) -> Result<()> {
    fs::DirBuilder::new().mode(0o700).create(path)?;
    Ok(())
}
pub(crate) fn sync_dir(path: &Path) -> Result<()> {
    File::open(path)?.sync_all()?;
    Ok(())
}
pub(crate) fn atomic_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let tmp = path.with_extension(format!(
        "{}.tmp",
        supabricks_core::resource::OperationId::new()
    ));
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&tmp)?;
    f.write_all(&serde_json::to_vec(value)?)?;
    f.sync_all()?;
    fs::rename(&tmp, path)?;
    sync_dir(path.parent().unwrap())
}
fn relative(name: &str) -> Result<()> {
    if name.is_empty()
        || name.len() > 4096
        || name.contains('\\')
        || !Path::new(name)
            .components()
            .all(|c| matches!(c, Component::Normal(_)))
    {
        return Err(invalid("unsafe recovery path"));
    }
    Ok(())
}
fn private_root(path: &Path) -> Result<PathBuf> {
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir()
        || m.uid() != unsafe { libc::geteuid() }
        || m.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid(
            "recovery directory must be owned by this user with mode 0700 and must not be a symlink",
        ));
    }
    Ok(path.canonicalize()?)
}
fn entry(path: &Path, destination: Option<&Path>) -> Result<Entry> {
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let before = input.metadata()?;
    if !before.is_file() {
        return Err(invalid("recovery payload must contain regular files"));
    }
    let mut output = destination
        .map(|p| {
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(p)
        })
        .transpose()?;
    let mut digest = Sha256::new();
    let mut bytes = 0;
    let mut buffer = [0; 1024 * 1024];
    loop {
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        digest.update(&buffer[..n]);
        bytes += n as u64;
        if let Some(out) = &mut output {
            out.write_all(&buffer[..n])?;
        }
    }
    if bytes != before.len() || input.metadata()?.modified()? != before.modified()? {
        return Err(conflict("recovery source changed while being read"));
    }
    if let Some(out) = output {
        out.sync_all()?;
    }
    Ok(Entry {
        bytes,
        sha256: hex::encode(digest.finalize()),
    })
}
fn excluded(name: &str) -> bool {
    // Recreated launch configuration and dead sockets carry no durable data.
    matches!(
        name,
        "owner.lock"
            | "control.sock"
            | "daemon.log"
            | "state.sqlite3-wal"
            | "state.sqlite3-shm"
            | "upgrade.json"
            | "restore-incomplete"
            | "process-compose.json"
            | "supervisor.token"
    ) || matches!(
        name.split('/').next(),
        Some("logs" | "tmp" | "launches" | "notebook-work")
    )
}
fn walk(
    root: &Path,
    at: &Path,
    dirs: &mut Vec<String>,
    files: &mut BTreeMap<String, Entry>,
    copy: Option<&Path>,
    source: bool,
    depth: usize,
) -> Result<()> {
    if depth > 64 || dirs.len() + files.len() > 1_000_000 {
        return Err(invalid("recovery inventory exceeds limits"));
    }
    let mut paths: Vec<_> = fs::read_dir(at)?
        .map(|e| e.map(|e| e.path()))
        .collect::<std::io::Result<_>>()?;
    paths.sort();
    for p in paths {
        let name = p
            .strip_prefix(root)
            .unwrap()
            .to_str()
            .ok_or_else(|| invalid("recovery paths must be UTF-8"))?
            .to_owned();
        relative(&name)?;
        if source && excluded(&name) {
            continue;
        }
        let m = fs::symlink_metadata(&p)?;
        if source && m.file_type().is_socket() {
            continue;
        }
        let parts: Vec<_> = name.split('/').collect();
        let shared_memory_link = source
            && m.file_type().is_symlink()
            && parts.len() == 4
            && parts[0] == "computes"
            && parts[1]
                .parse::<supabricks_core::resource::EndpointId>()
                .is_ok()
            && parts[2] == "pgdata"
            && parts[3] == "pg_dynshmem"
            && fs::read_link(&p)? == Path::new("/dev/shm");
        if shared_memory_link {
            // Neon maps PostgreSQL dynamic shared memory to host tmpfs. No
            // process survives this boundary; never copy or traverse host shm.
            if let Some(to) = copy {
                private_dir(&to.join(&name))?;
                sync_dir(&to.join(&name))?;
            }
            dirs.push(name);
        } else if m.is_dir() {
            if let Some(to) = copy {
                private_dir(&to.join(&name))?;
            }
            dirs.push(name.clone());
            walk(root, &p, dirs, files, copy, source, depth + 1)?;
            if let Some(to) = copy {
                sync_dir(&to.join(&name))?;
            }
        } else if m.is_file() {
            files.insert(
                name.clone(),
                entry(&p, copy.map(|to| to.join(name)).as_deref())?,
            );
        } else {
            return Err(invalid(format!(
                "recovery payload contains a symlink or special file: {name}"
            )));
        }
    }
    Ok(())
}
/// No migration or generation increment. Hold the normal daemon lock while
/// checkpointing and reading every byte; never snapshot SQLite under a writer.
pub(crate) struct Stopped {
    pub root: PathBuf,
    pub(crate) db: Connection,
    _owner: DataRoot,
}
impl Stopped {
    pub(crate) fn open(root: &Path) -> Result<Self> {
        Self::open_schema(root, SCHEMA_VERSION)
    }
    pub(crate) fn open_schema(root: &Path, expected: u32) -> Result<Self> {
        if !matches!(expected, 8 | 9) {
            return Err(conflict("unsupported recovery schema"));
        }
        let root = private_root(root)?;
        let owner = DataRoot::acquire(&root)?;
        let path = root.join("state.sqlite3");
        let meta = fs::symlink_metadata(&path)?;
        if !meta.is_file() || meta.nlink() != 1 || meta.permissions().mode() & 0o077 != 0 {
            return Err(invalid("invalid private metadata database"));
        }
        let db = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        let schema: u32 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
        if schema != expected {
            return Err(conflict(
                "recovery requires the supported catalog schema; no implicit migration",
            ));
        }
        if db.query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))? != "ok"
            || db.prepare("PRAGMA foreign_key_check")?.exists([])?
        {
            return Err(conflict("metadata integrity check failed"));
        }
        for table in ["native_processes", "processes"] {
            if db.prepare(&format!("SELECT 1 FROM {table}"))?.exists([])? {
                return Err(conflict(
                    "recorded processes remain; stop the owning release before recovery",
                ));
            }
        }
        if db
            .prepare("SELECT 1 FROM analytical_sessions WHERE state NOT IN ('closed','failed')")?
            .exists([])?
        {
            return Err(conflict(
                "analytical sessions remain; complete shutdown first",
            ));
        }
        if schema == 9 && db.prepare("SELECT 1 FROM ingest_jobs WHERE state IN ('loading','reconciling') OR worker IS NOT NULL")?.exists([])? {
            return Err(conflict("imports require receipt reconciliation before backup; reopen the owning runtime and resolve pending jobs"));
        }
        if schema == 9 {
            validate_ingest(&root, &db)?;
        }
        db.pragma_update(None, "synchronous", "FULL")?;
        let busy: i64 = db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| r.get(0))?;
        if busy != 0 {
            return Err(conflict("metadata checkpoint is busy"));
        }
        Ok(Self {
            root,
            db,
            _owner: owner,
        })
    }
    pub(crate) fn validate_pg(&self) -> Result<()> {
        if self
            .db
            .prepare("SELECT 1 FROM endpoints WHERE pg_major != 17")?
            .exists([])?
        {
            return Err(conflict("unsupported PostgreSQL catalog version"));
        }
        Ok(())
    }
}
fn new_destination(destination: &Path, source: &Path) -> Result<PathBuf> {
    if fs::symlink_metadata(destination).is_ok() {
        return Err(conflict(
            "recovery destination already exists; choose a new directory",
        ));
    }
    let parent = destination
        .parent()
        .ok_or_else(|| invalid("recovery destination needs a parent"))?
        .canonicalize()?;
    let path = parent.join(
        destination
            .file_name()
            .ok_or_else(|| invalid("missing destination name"))?,
    );
    if path.starts_with(source) {
        return Err(invalid("recovery destination must be outside its source"));
    }
    Ok(path)
}
pub fn create(root: &Path, destination: &Path) -> Result<Value> {
    let root = private_root(root)?;
    new_destination(destination, &root)?;
    crate::installation::check_data_root(&root)?;
    crate::runtime_cli::shutdown(&root)?;
    let stopped = Stopped::open(&root)?;
    let release = Installation::discover()?
        .as_ref()
        .map(Release::of)
        .transpose()?;
    let manifest = create_locked(&stopped, destination, release)?;
    Ok(
        json!({"backup":destination,"id":manifest.id,"verified":true,"runtime":"stopped","contains_credentials":true,"files":manifest.files.len()}),
    )
}
pub(crate) fn create_locked(
    stopped: &Stopped,
    destination: &Path,
    release: Option<Release>,
) -> Result<Manifest> {
    let destination = new_destination(destination, &stopped.root)?;
    stopped.validate_pg()?;
    if stopped.root.join("runtime.json").exists() {
        let cfg: crate::engine::RuntimeConfig =
            serde_json::from_slice(&fs::read(stopped.root.join("runtime.json"))?)?;
        if cfg.version != 2 {
            return Err(conflict("unsupported runtime format"));
        }
        if cfg.installation_identity.as_deref() != release.as_ref().map(|r| r.identity.as_str()) {
            return Err(conflict(
                "recovery source no longer matches the release being backed up",
            ));
        }
        if let Some(tls) = cfg.compute_tls {
            for path in [tls.certificate, tls.key] {
                if !path.canonicalize()?.starts_with(&stopped.root) {
                    return Err(invalid(
                        "TLS material must be inside the data root to include it in recovery",
                    ));
                }
            }
        }
    }
    private_dir(&destination)?;
    sync_dir(destination.parent().unwrap())?;
    let data = destination.join("data");
    private_dir(&data)?;
    let mut manifest = Manifest {
        format_version: 1,
        id: supabricks_core::resource::OperationId::new().to_string(),
        consistency: "stopped-cell".into(),
        source_root: stopped.root.clone(),
        schema_version: stopped
            .db
            .pragma_query_value(None, "user_version", |r| r.get(0))?,
        release,
        directories: vec![],
        files: BTreeMap::new(),
    };
    walk(
        &stopped.root,
        &stopped.root,
        &mut manifest.directories,
        &mut manifest.files,
        Some(&data),
        true,
        0,
    )?;
    sync_dir(&data)?;
    atomic_json(&destination.join("backup.json"), &manifest)?;
    verify(&destination)?;
    Ok(manifest)
}
pub fn verify(path: &Path) -> Result<Manifest> {
    let root = private_root(path)?;
    let manifest_file = root.join("backup.json");
    let m = fs::symlink_metadata(&manifest_file)?;
    if !m.is_file() || m.len() > 128 * 1024 * 1024 || m.permissions().mode() & 0o077 != 0 {
        return Err(invalid("invalid private recovery manifest"));
    }
    let manifest: Manifest = serde_json::from_slice(&fs::read(manifest_file)?)?;
    if manifest.format_version != 1
        || !matches!(manifest.schema_version, 8 | 9)
        || manifest.consistency != "stopped-cell"
        || !manifest.source_root.is_absolute()
    {
        return Err(invalid("unsupported recovery bundle"));
    }
    for name in manifest.files.keys().chain(manifest.directories.iter()) {
        relative(name)?;
    }
    if !manifest.files.contains_key("state.sqlite3") {
        return Err(invalid("recovery bundle lacks control metadata"));
    }
    let data = private_root(&root.join("data"))?;
    let mut dirs = vec![];
    let mut files = BTreeMap::new();
    walk(&data, &data, &mut dirs, &mut files, None, false, 0)?;
    if dirs != manifest.directories || files != manifest.files {
        return Err(invalid("recovery inventory checksum mismatch"));
    }
    // A sealed, checkpointed bundle has no WAL. Immutable mode prevents SQLite
    // from creating WAL/SHM sidecars while verifying the backup inventory.
    let uri = format!(
        "file:{}?immutable=1",
        percent_encoding::utf8_percent_encode(
            data.join("state.sqlite3")
                .to_str()
                .ok_or_else(|| invalid("backup path must be UTF-8"))?,
            percent_encoding::NON_ALPHANUMERIC
        )
    );
    let db = Connection::open_with_flags(
        uri,
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    if db.pragma_query_value(None, "user_version", |r| r.get::<_, u32>(0))?
        != manifest.schema_version
        || db.query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))? != "ok"
        || db.prepare("PRAGMA foreign_key_check")?.exists([])?
    {
        return Err(invalid(
            "recovery catalog format or integrity differs from its manifest",
        ));
    }
    if manifest.schema_version == 9 {
        validate_ingest(&data, &db)?;
    }
    drop(db);
    if manifest.files.contains_key("runtime.json") {
        let runtime: crate::engine::RuntimeConfig =
            serde_json::from_slice(&fs::read(data.join("runtime.json"))?)?;
        if runtime.version != 2
            || runtime.installation_identity.as_deref()
                != manifest.release.as_ref().map(|r| r.identity.as_str())
        {
            return Err(invalid("recovery runtime and release identities disagree"));
        }
    }
    Ok(manifest)
}
pub fn restore(backup: &Path, destination: &Path) -> Result<Value> {
    restore_with_release(backup, destination, None)
}
pub fn restore_with_release(
    backup: &Path,
    destination: &Path,
    release: Option<&Path>,
) -> Result<Value> {
    let backup = private_root(backup)?;
    let manifest = verify(&backup)?;
    let installed = match release {
        Some(path) => Installation::at_executable(&path.join("bin/supabricks").canonicalize()?)?,
        None => Installation::discover()?,
    };
    match (&manifest.release, &installed) {
        (Some(expected), Some(actual)) if expected == &Release::of(actual)? => actual.verify()?,
        (None, None) => {}
        _ => {
            return Err(conflict(
                "restore requires the exact source release and target; restore first, then upgrade",
            ));
        }
    }
    if installed
        .as_ref()
        .map(|i| formats(i)["local_catalog"].as_u64())
        .unwrap_or(Some(SCHEMA_VERSION as u64))
        != Some(manifest.schema_version as u64)
    {
        return Err(conflict(
            "backup catalog differs from its source release format",
        ));
    }
    let destination = new_destination(destination, &backup)?;
    private_dir(&destination)?;
    let owner = DataRoot::acquire(&destination)?;
    atomic_json(
        &destination.join("restore-incomplete"),
        &json!({"backup_id":manifest.id}),
    )?;
    // A crash leaves this guard and a private partial root. Never overwrite it;
    // another fresh destination can always be restored from the intact bundle.
    for dir in &manifest.directories {
        private_dir(&destination.join(dir))?;
    }
    for (name, expected) in &manifest.files {
        if entry(
            &backup.join("data").join(name),
            Some(&destination.join(name)),
        )? != *expected
        {
            return Err(invalid("backup changed during restore"));
        }
    }
    for dir in manifest.directories.iter().rev() {
        sync_dir(&destination.join(dir))?;
    }
    let runtime = destination.join("runtime.json");
    if runtime.exists() {
        let mut cfg: crate::engine::RuntimeConfig = serde_json::from_slice(&fs::read(&runtime)?)?;
        if let Some(tls) = &mut cfg.compute_tls {
            for path in [&mut tls.certificate, &mut tls.key] {
                *path = destination.join(
                    path.strip_prefix(&manifest.source_root)
                        .map_err(|_| invalid("TLS path is outside backup root"))?,
                );
            }
            atomic_json(&runtime, &cfg)?;
        }
    }
    // Validate restored metadata without opening a daemon or running migrations.
    let db = Connection::open_with_flags(
        destination.join("state.sqlite3"),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    if db.query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))? != "ok"
        || db.prepare("PRAGMA foreign_key_check")?.exists([])?
    {
        return Err(invalid("restored metadata integrity check failed"));
    }
    drop(db);
    atomic_json(
        &destination.join("restore.json"),
        &json!({"backup_id":manifest.id,"source_root":manifest.source_root,"credentials_restored":true}),
    )?;
    fs::remove_file(destination.join("restore-incomplete"))?;
    sync_dir(&destination)?;
    sync_dir(destination.parent().unwrap())?;
    drop(owner);
    Ok(
        json!({"restored":true,"backup_id":manifest.id,"data_dir":destination,"runtime":"stopped","credentials_restored":true}),
    )
}

pub(crate) fn file_hash(path: &Path) -> Result<String> {
    Ok(entry(path, None)?.sha256)
}

/// Payloads are durable data, not optional cache. Validate every staged reference
/// before publishing a stopped backup and again before restoring one.
fn validate_ingest(root: &Path, db: &Connection) -> Result<()> {
    if db.prepare("SELECT 1 FROM ingest_sources WHERE state='receiving' UNION ALL SELECT 1 FROM ingest_jobs WHERE state IN ('loading','reconciling') OR worker IS NOT NULL")?.exists([])? {
        return Err(conflict("interrupted acquisition or import requires owning-runtime recovery before backup"));
    }
    if db.prepare("SELECT 1 FROM ingest_jobs j JOIN ingest_sources s ON s.id=j.source_id WHERE j.source_released=0 AND s.state!='staged'")?.exists([])? {
        return Err(conflict("retained ingestion job is missing its immutable source"));
    }
    let mut q = db.prepare("SELECT id,bytes,sha256 FROM ingest_sources WHERE state='staged'")?;
    let rows = q.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, String>(2)?,
        ))
    })?;
    for row in rows {
        let (id, bytes, sha) = row?;
        let id: supabricks_core::resource::OperationId =
            id.parse().map_err(|_| invalid("invalid source ID"))?;
        let path = root.join("ingest/sources").join(format!("{id}.source"));
        let value = entry(&path, None)?;
        if value.bytes != bytes as u64 || value.sha256 != sha {
            return Err(conflict("staged source content differs from catalog"));
        }
    }
    Ok(())
}
