//! Incremental file verification, durable generation promotion and owned GC.
//! SQLite is the only publication authority; directory presence never publishes.
use crate::store::{
    Publication, Result, Store,
    error::{conflict, invalid},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Component, Path, PathBuf},
};
use supabricks_core::resource::OperationId;

const MAX_MANIFEST: u64 = 2 * 1024 * 1024;
const CHUNK: usize = 1024 * 1024;
const PER_TICK: usize = 4 * CHUNK;
#[derive(Default)]
pub struct Publisher {
    verifier: Option<Verifier>,
    verification_progress: bool,
    verified_files: BTreeMap<PathBuf, (FileStamp, String)>,
    pub last_error: Option<String>,
    pub recovery: Value,
}
struct Verifier {
    id: OperationId,
    root: PathBuf,
    manifest: Value,
    manifest_hash: String,
    files: Vec<FileCheck>,
    index: usize,
    current: Option<(File, u64, Sha256, FileStamp)>,
    reuse_verified: bool,
}

// Content evidence only: never a persisted ownership or durability token.
// Replacement, in-place writes (including restored mtime), permissions and
// hardlink changes invalidate it. New daemons always start with an empty cache.
#[derive(Clone, PartialEq, Eq)]
struct FileStamp {
    values: [u64; 7],
    times: [i64; 4],
}
impl FileStamp {
    fn cacheable(&self) -> bool {
        self.values[3] & 0o022 == 0 && self.values[4] == u64::from(unsafe { libc::geteuid() })
    }
    fn of(m: &fs::Metadata) -> Self {
        Self {
            values: [
                m.dev(),
                m.ino(),
                m.len(),
                u64::from(m.mode()),
                u64::from(m.uid()),
                u64::from(m.gid()),
                m.nlink(),
            ],
            times: [m.mtime(), m.mtime_nsec(), m.ctime(), m.ctime_nsec()],
        }
    }
}
struct FileCheck {
    path: String,
    bytes: u64,
    hash: String,
    needs_sync: bool,
}
fn require(ok: bool, message: &str) -> Result<()> {
    if ok { Ok(()) } else { Err(invalid(message)) }
}
fn small(path: &Path) -> Result<Vec<u8>> {
    require(
        fs::symlink_metadata(path)?.is_file(),
        "manifest must be a regular file",
    )?;
    let mut bytes = Vec::new();
    File::open(path)?
        .take(MAX_MANIFEST + 1)
        .read_to_end(&mut bytes)?;
    require(
        bytes.len() as u64 <= MAX_MANIFEST,
        "snapshot manifest exceeds 2 MiB",
    )?;
    Ok(bytes)
}
fn hash(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
fn directory(path: &Path) -> Result<()> {
    if !path.exists() {
        fs::create_dir(path)?;
        File::open(path.parent().unwrap())?.sync_all()?;
    }
    require(
        fs::symlink_metadata(path)?.is_dir(),
        "analytics path must be a directory, not a symlink",
    )
}
fn roots(store: &Store) -> Result<(PathBuf, PathBuf)> {
    let root = store.root().join("analytics");
    directory(&root)?;
    let stage = root.join("staging");
    directory(&stage)?;
    let generations = root.join("generations");
    directory(&generations)?;
    Ok((stage, generations))
}
fn files(
    root: &Path,
    at: &Path,
    depth: usize,
    out: &mut BTreeSet<String>,
    entries: &mut usize,
) -> Result<()> {
    require(depth <= 4, "generation nesting exceeds limit")?;
    for entry in fs::read_dir(at)? {
        *entries += 1;
        require(
            *entries <= 8192,
            "generation directory entry limit exceeded",
        )?;
        let path = entry?.path();
        let meta = fs::symlink_metadata(&path)?;
        if meta.is_dir() {
            files(root, &path, depth + 1, out, entries)?;
        } else {
            require(
                meta.is_file(),
                "generation contains a symlink or non-regular file",
            )?;
            let relative = path
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .ok_or_else(|| invalid("non-UTF8 generation path"))?
                .to_string();
            if relative != "snapshot.json" && relative != "snapshot.tmp" {
                out.insert(relative);
            }
            require(out.len() <= 4097, "generation file count exceeds limit")?;
        }
    }
    Ok(())
}
fn layout(root: &Path, manifest: &Value) -> Result<Vec<FileCheck>> {
    require(
        fs::symlink_metadata(root)?.is_dir(),
        "generation root must be a directory",
    )?;
    let list = manifest["files"]
        .as_array()
        .ok_or_else(|| invalid("manifest requires files"))?;
    require(list.len() <= 4096, "manifest file count exceeds limit")?;
    let tables = manifest["tables"]
        .as_array()
        .ok_or_else(|| invalid("manifest requires tables"))?;
    require(tables.len() <= 128, "manifest table count exceeds limit")?;
    let mut table_paths = BTreeSet::new();
    let mut oids = BTreeSet::new();
    let mut names = BTreeSet::new();
    for t in tables {
        let oid = t["oid"]
            .as_u64()
            .ok_or_else(|| invalid("missing table OID"))?;
        require(
            oid > 0 && oid <= u32::MAX as u64 && oids.insert(oid),
            "invalid or duplicate OID",
        )?;
        let path = t["path"]
            .as_str()
            .ok_or_else(|| invalid("missing table path"))?;
        require(
            path == oid.to_string() && table_paths.insert(path.to_string()),
            "invalid table path",
        )?;
        require(
            t["schema"].as_str().is_some_and(|s| !s.is_empty())
                && t["name"].as_str().is_some_and(|s| !s.is_empty())
                && names.insert(json!([t["schema"], t["name"]]).to_string()),
            "invalid or duplicate table name",
        )?;
        require(
            t["columns"]
                .as_array()
                .is_some_and(|c| !c.is_empty() && c.len() <= 128)
                && t["rows"].as_u64().is_some(),
            "invalid table schema/count",
        )?;
        let version = t["version"]
            .as_u64()
            .ok_or_else(|| invalid("missing Delta version"))?;
        require(version < 1024, "Delta version exceeds metadata budget")?;
    }
    let mut expected = BTreeSet::from(["manifest.json".to_string()]);
    let mut checks = Vec::new();
    for f in list {
        let path = f["path"]
            .as_str()
            .ok_or_else(|| invalid("missing file path"))?;
        let parts: Vec<_> = Path::new(path).components().collect();
        require(
            path.len() <= 256
                && !path.contains('\\')
                && parts.iter().all(|p| matches!(p, Component::Normal(_)))
                && (2..=3).contains(&parts.len()),
            "unsafe generation-relative file path",
        )?;
        let first = parts[0].as_os_str().to_str().unwrap();
        require(
            table_paths.contains(first),
            "file belongs to an unknown table",
        )?;
        require(expected.insert(path.to_string()), "duplicate file path")?;
        let bytes = f["bytes"]
            .as_u64()
            .ok_or_else(|| invalid("missing file size"))?;
        let digest = f["sha256"]
            .as_str()
            .ok_or_else(|| invalid("missing checksum"))?;
        require(
            digest.len() == 64 && digest.bytes().all(|b| b.is_ascii_hexdigit()),
            "invalid checksum",
        )?;
        checks.push(FileCheck {
            path: path.into(),
            bytes,
            hash: digest.into(),
            needs_sync: true,
        });
    }
    let mut actual = BTreeSet::new();
    files(root, root, 0, &mut actual, &mut 0)?;
    require(
        actual == expected,
        "manifest does not describe the exact generation file set",
    )?;
    for t in tables {
        for version in 0..=t["version"].as_u64().unwrap() {
            require(
                expected.contains(&format!(
                    "{}/_delta_log/{version:020}.json",
                    t["path"].as_str().unwrap()
                )),
                "incomplete Delta transaction log",
            )?;
        }
    }
    for f in &checks {
        require(
            fs::metadata(root.join(&f.path))?.len() == f.bytes,
            "generation file size differs from manifest",
        )?;
    }
    Ok(checks)
}
impl Verifier {
    fn open(store: &Store, p: &Publication, root: PathBuf) -> Result<Self> {
        let e = store.export(p.export_id)?;
        let source = store.branch(e.source_id)?;
        let child = store.branch(e.child_id)?;
        let bytes = small(&root.join("manifest.json"))?;
        let m: Value = serde_json::from_slice(&bytes)?;
        require(
            m["format_version"] == 1
                && m["status"] == "files_complete"
                && m["published"] == false
                && m["id"] == json!(e.id),
            "invalid completed-export manifest",
        )?;
        let identity = json!({"project_id":e.project_id,"branch_id":e.source_id,"timeline_id":source.branch.timeline_id,"tenant_id":child.branch.tenant_id,"export_branch_id":e.child_id,"export_timeline_id":child.branch.timeline_id,"lsn":child.branch.ancestor_lsn});
        require(
            m["source"] == identity && child.branch.ancestor_lsn.is_some(),
            "manifest differs from captured source identity",
        )?;
        require(
            m["database"] == "postgres"
                && m["database_oid"].as_u64().is_some()
                && m["versions"].is_object()
                && m["transaction"] == "REPEATABLE READ READ ONLY",
            "invalid export database/engine metadata",
        )?;
        let checks = layout(&root, &m)?;
        let used = checks
            .iter()
            .try_fold(bytes.len() as u64, |sum, f| sum.checked_add(f.bytes))
            .ok_or_else(|| invalid("manifest size overflow"))?;
        require(
            used <= e.limits.max_bytes,
            "generation exceeds admitted output budget",
        )?;
        Ok(Self {
            id: p.export_id,
            root,
            manifest: m,
            manifest_hash: hash(&bytes),
            files: checks,
            index: 0,
            current: None,
            reuse_verified: false,
        })
    }
    fn advance(
        &mut self,
        cache: &mut BTreeMap<PathBuf, (FileStamp, String)>,
        hook: &mut impl FnMut(&str) -> Result<()>,
    ) -> Result<bool> {
        let _profile = crate::sync_profile::span("publication.verify");
        let mut budget = PER_TICK;
        let mut buffer = vec![0; CHUNK];
        let mut entries = 64;
        while self.index < self.files.len() && budget > 0 && entries > 0 {
            let check = &self.files[self.index];
            let path = self.root.join(&check.path);
            if self.current.is_none() {
                let f = OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_NOFOLLOW)
                    .open(&path)?;
                let metadata = f.metadata()?;
                require(metadata.is_file(), "generation file is not regular")?;
                let stamp = FileStamp::of(&metadata);
                if self.reuse_verified
                    && stamp.cacheable()
                    && !check.needs_sync
                    && cache
                        .get(&path)
                        .is_some_and(|(old, hash)| old == &stamp && hash == &check.hash)
                {
                    require(
                        FileStamp::of(&fs::symlink_metadata(&path)?) == stamp,
                        "generation changed during verification",
                    )?;
                    self.index += 1;
                    entries -= 1;
                    hook(&format!("verified_file:{}", self.index))?;
                    continue;
                }
                self.current = Some((f, 0, Sha256::new(), stamp));
            }
            let (file, read, digest, stamp) = self.current.as_mut().unwrap();
            let n = file.read(&mut buffer[..budget.min(CHUNK)])?;
            if n == 0 {
                require(
                    *read == check.bytes && hex::encode(digest.clone().finalize()) == check.hash,
                    "generation checksum mismatch",
                )?;
                require(
                    FileStamp::of(&file.metadata()?) == *stamp
                        && FileStamp::of(&fs::symlink_metadata(&path)?) == *stamp,
                    "generation changed during verification",
                )?;
                if check.needs_sync {
                    {
                        let _profile = crate::sync_profile::span("publication.file_fsync");
                        file.sync_all()?;
                    }
                    hook(&format!("synced_file:{}", self.index + 1))?;
                }
                if self.reuse_verified && stamp.cacheable() {
                    if cache.len() >= 4096 && !cache.contains_key(&path) {
                        cache.clear();
                    }
                    cache.insert(path, (stamp.clone(), check.hash.clone()));
                }
                self.current = None;
                self.index += 1;
                entries -= 1;
                hook(&format!("verified_file:{}", self.index))?;
                // Empty files must also consume this tick's bounded budget.
                budget = budget.saturating_sub(1);
            } else {
                digest.update(&buffer[..n]);
                *read += n as u64;
                budget -= n;
                require(*read <= check.bytes, "generation grew during verification")?;
            }
        }
        Ok(self.index == self.files.len())
    }
}
fn atomic_descriptor(
    root: &Path,
    value: &Value,
    hook: &mut impl FnMut(&str) -> Result<()>,
) -> Result<()> {
    let _profile = crate::sync_profile::span("publication.descriptor");
    File::open(root.join("manifest.json"))?.sync_all()?;
    let bytes = serde_json::to_vec_pretty(value)?;
    require(
        bytes.len() as u64 <= MAX_MANIFEST - 16384,
        "snapshot descriptor exceeds 2 MiB",
    )?;
    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(root.join("snapshot.tmp"))?;
    file.write_all(&bytes)?;
    hook("descriptor_written")?;
    file.sync_all()?;
    fs::rename(root.join("snapshot.tmp"), root.join("snapshot.json"))?;
    // All nested directories must be durable before the top-level rename.
    fn sync_dirs(path: &Path) -> Result<()> {
        for e in fs::read_dir(path)? {
            let p = e?.path();
            if fs::symlink_metadata(&p)?.is_dir() {
                sync_dirs(&p)?;
            }
        }
        File::open(path)?.sync_all()?;
        Ok(())
    }
    sync_dirs(root)?;
    hook("descriptor_durable")?;
    Ok(())
}
pub(crate) fn check_ready(root: &Path, d: &Value) -> Result<()> {
    require(
        serde_json::from_slice::<Value>(&small(&root.join("snapshot.json"))?)? == *d,
        "snapshot descriptor differs from journal",
    )?;
    let bytes = small(&root.join("manifest.json"))?;
    require(
        d["manifest_sha256"] == hash(&bytes)
            && serde_json::from_slice::<Value>(&bytes)? == d["manifest"],
        "export manifest differs from journal",
    )?;
    if d["format_version"] == 2 {
        let installation = root
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .ok_or_else(|| invalid("invalid epoch location"))?;
        crate::analytics_v2::layout(installation, d)?;
    } else {
        layout(root, &d["manifest"])?;
    }
    Ok(())
}
impl Publisher {
    pub(crate) fn verification_pending(&self) -> bool {
        // A stale capture can leave a verifier parked. Only a turn that actually
        // advanced bytes is immediately runnable; blocked work must not spin.
        self.verifier.is_some() && self.verification_progress
    }

    pub fn recover(store: &mut Store) -> Result<Self> {
        let (stage, generations) = roots(store)?;
        let mut untracked = 0;
        for root in [&stage, &generations] {
            for entry in fs::read_dir(root)?.take(4097) {
                let name = entry?.file_name();
                let known = name
                    .to_str()
                    .and_then(|s| s.parse::<OperationId>().ok())
                    .is_some_and(|id| {
                        store.export(id).is_ok()
                            || store.is_incremental_artifact(id).unwrap_or(false)
                    });
                if !known {
                    untracked += 1;
                }
            }
        }
        let mut unavailable = 0;
        // Do not silently republish directories or substitute an older snapshot.
        // Verify metadata, exact file set and sizes; full checksums are streamed
        // before initial publication. External post-publication bit rot needs a
        // recovery bundle (reader checksums can diagnose it independently).
        for (project, id) in store.recoverable_snapshot_ids()? {
            let s = store.snapshot_record(project, id, true)?;
            let result = check_ready(
                &generations.join(s.publication.export_id.to_string()),
                s.publication
                    .descriptor
                    .as_ref()
                    .ok_or_else(|| conflict("published descriptor missing"))?,
            );
            if let Err(error) = result {
                unavailable += 1;
                store.unavailable_snapshot(s.publication.epoch_id, &error.to_string())?;
            } else {
                store.restored_snapshot(s.publication.epoch_id)?;
            }
        }
        Ok(Self {
            recovery: json!({"observed_at_ms":chrono::Utc::now().timestamp_millis(),"untracked_entries_retained":untracked,"unavailable_snapshots":unavailable}),
            ..Self::default()
        })
    }
    pub fn tick(&mut self, store: &mut Store) -> Result<()> {
        let _profile = crate::sync_profile::span("publication.tick");
        self.tick_with_hook(store, &mut |_| Ok(()))
    }
    /// Hook is used by subprocess crash tests, never selected through public IPC.
    pub fn tick_with_hook(
        &mut self,
        store: &mut Store,
        hook: &mut impl FnMut(&str) -> Result<()>,
    ) -> Result<()> {
        self.verification_progress = false;
        let (stage, generations) = roots(store)?;
        let pending = store.pending_publications()?;
        if self.verifier.as_ref().is_some_and(|v| {
            !pending
                .iter()
                .any(|p| p.export_id == v.id && p.state == "requested")
        }) {
            self.verifier = None;
        }
        if let Some(p) = pending.first() {
            let result = (|| -> Result<()> {
                if store.is_incremental_artifact(p.export_id)? {
                    return self.tick_incremental(store, p, &stage, &generations, hook);
                }
                if p.state == "requested" {
                    if self.verifier.is_none() {
                        self.verifier = Some(Verifier::open(
                            store,
                            p,
                            stage.join(p.export_id.to_string()),
                        )?);
                    }
                    let v = self.verifier.as_mut().unwrap();
                    self.verification_progress = true;
                    if v.advance(&mut self.verified_files, hook)? {
                        let descriptor = json!({"format_version":1,"installation_id":store.installation_id()?,"epoch_id":p.epoch_id,"ordinal":p.ordinal,"export_id":p.export_id,"source_revision":p.source_revision,"prepared_at_ms":chrono::Utc::now().timestamp_millis(),"generation":format!("analytics/generations/{}",p.export_id),"manifest_sha256":v.manifest_hash,"manifest":v.manifest});
                        atomic_descriptor(&v.root, &descriptor, hook)?;
                        store.publication_ready(p, &descriptor)?;
                        hook("files_complete")?;
                        self.verifier = None;
                    }
                } else {
                    let from = stage.join(p.export_id.to_string());
                    let to = generations.join(p.export_id.to_string());
                    let d = p
                        .descriptor
                        .as_ref()
                        .ok_or_else(|| conflict("ready descriptor missing"))?;
                    require(
                        !(from.exists() && to.exists()),
                        "duplicate generation directories",
                    )?;
                    check_ready(if to.exists() { &to } else { &from }, d)?;
                    hook("before_rename")?;
                    if !to.exists() {
                        fs::rename(&from, &to)?;
                    }
                    File::open(&stage)?.sync_all()?;
                    File::open(&generations)?.sync_all()?;
                    hook("after_rename")?;
                    hook("before_commit")?;
                    store.commit_publication(p)?;
                    hook("after_commit")?;
                }
                Ok(())
            })();
            if let Err(e) = result {
                self.verifier = None;
                self.verified_files.clear();
                // If commit succeeded, an injected post-commit error cannot
                // undo publication or authorize deletion of the live epoch.
                if store.publication(p.export_id)?.state != "published" {
                    if store.is_incremental_artifact(p.export_id)? {
                        let project = store.branch(p.branch_id)?.branch.project_id;
                        let mut run = store.incremental_run(project, p.export_id)?;
                        store.fail_incremental(
                            &mut run,
                            "incremental_publication_failed",
                            false,
                        )?;
                    } else {
                        store.fail_publication(p.export_id, &e.to_string())?;
                    }
                }
                return Err(e);
            }
        }
        if let Some(id) = store.pending_analytics_gc()?.first() {
            hook("before_gc")?;
            for root in [&stage, &generations] {
                let path = root.join(id.to_string());
                if path.exists() {
                    require(
                        fs::symlink_metadata(&path)?.is_dir(),
                        "refuse non-directory GC target",
                    )?;
                    fs::remove_dir_all(path)?;
                    File::open(root)?.sync_all()?;
                }
            }
            hook("after_gc_files")?;
            store.finish_analytics_gc(*id)?;
            hook("after_gc_commit")?;
        }
        Ok(())
    }
}

impl Publisher {
    fn tick_incremental(
        &mut self,
        store: &mut Store,
        p: &Publication,
        stage: &Path,
        generations: &Path,
        hook: &mut impl FnMut(&str) -> Result<()>,
    ) -> Result<()> {
        let Some(d) = p.descriptor.as_ref() else {
            return Ok(());
        };
        let project = store.branch(p.branch_id)?.branch.project_id;
        let mut run = store.incremental_run(project, p.export_id)?;
        let capture = store.incremental_live(&run)?;
        // A short source outage does not publish from a stale status observation.
        if capture.state != "capturing"
            || capture
                .observed_at_ms
                .is_none_or(|at| chrono::Utc::now().timestamp_millis() - at > 5000)
        {
            return Ok(());
        }
        if p.state == "requested" {
            if self.verifier.is_none() {
                // Published files in this storage generation are immutable and
                // already durable. Reuse only this daemon's verified file identity
                // and change metadata; a descriptor checksum alone never suffices.
                let mut durable = BTreeSet::new();
                if let Some(epoch) = run.previous_epoch {
                    let old = store.snapshot(project, epoch)?;
                    if old.publication.state == "published"
                        && let Some(previous) = old.publication.descriptor
                        && previous["generation"] == d["generation"]
                        && previous["manifest"]["capture_identity"]
                            == d["manifest"]["capture_identity"]
                    {
                        durable.extend(crate::analytics_v2::layout(store.root(), &previous)?);
                    }
                }
                let files = crate::analytics_v2::layout(store.root(), d)?
                    .into_iter()
                    .map(|entry| {
                        let needs_sync = !durable.contains(&entry);
                        let (path, bytes, hash) = entry;
                        FileCheck {
                            path,
                            bytes,
                            hash,
                            needs_sync,
                        }
                    })
                    .collect();
                self.verifier = Some(Verifier {
                    id: p.export_id,
                    root: crate::analytics_v2::data_root(store.root(), d)?,
                    manifest: d["manifest"].clone(),
                    manifest_hash: d["manifest_sha256"].as_str().unwrap_or("").into(),
                    files,
                    index: 0,
                    current: None,
                    reuse_verified: true,
                });
            }
            self.verification_progress = true;
            if self
                .verifier
                .as_mut()
                .unwrap()
                .advance(&mut self.verified_files, hook)?
            {
                atomic_descriptor(&stage.join(p.export_id.to_string()), d, hook)?;
                store.publication_ready(p, d)?;
                self.verifier = None;
                hook("files_complete")?;
            } else {
                return Ok(());
            }
        }
        // Once verification is complete, the persisted ready state can be
        // committed immediately. Recovery still enters here for ready records;
        // commit_incremental rechecks source, policy and head fencing.
        let from = stage.join(p.export_id.to_string());
        let to = generations.join(p.export_id.to_string());
        require(
            !(from.exists() && to.exists()),
            "duplicate epoch directories",
        )?;
        check_ready(if to.exists() { &to } else { &from }, d)?;
        hook("before_rename")?;
        if !to.exists() {
            fs::rename(&from, &to)?;
        }
        File::open(stage)?.sync_all()?;
        File::open(generations)?.sync_all()?;
        hook("after_rename")?;
        hook("before_commit")?;
        store.commit_incremental(&mut run, d)?;
        hook("after_commit")?;
        Ok(())
    }
}

#[cfg(test)]
mod verification_cache_tests {
    use super::*;
    use std::fs::FileTimes;
    use std::os::unix::fs::PermissionsExt;

    fn verifier(root: &Path, bytes: &[u8], needs_sync: bool) -> Verifier {
        Verifier {
            id: OperationId::new(),
            root: root.to_owned(),
            manifest: json!({}),
            manifest_hash: String::new(),
            files: vec![FileCheck {
                path: "data".into(),
                bytes: bytes.len() as u64,
                hash: hash(bytes),
                needs_sync,
            }],
            index: 0,
            current: None,
            reuse_verified: true,
        }
    }
    fn finish(
        v: &mut Verifier,
        cache: &mut BTreeMap<PathBuf, (FileStamp, String)>,
    ) -> Result<usize> {
        for turn in 1..=10 {
            if v.advance(cache, &mut |_| Ok(()))? {
                return Ok(turn);
            }
        }
        panic!("verification failed to make bounded progress")
    }
    #[test]
    fn unchanged_published_bytes_reuse_but_new_files_and_restart_hash_fully() {
        let root = tempfile::tempdir().unwrap();
        let bytes = vec![42; PER_TICK + 1];
        fs::write(root.path().join("data"), &bytes).unwrap();
        fs::set_permissions(root.path().join("data"), fs::Permissions::from_mode(0o600)).unwrap();
        let mut cache = BTreeMap::new();
        assert_eq!(
            finish(&mut verifier(root.path(), &bytes, true), &mut cache).unwrap(),
            2
        );
        assert_eq!(
            finish(&mut verifier(root.path(), &bytes, false), &mut cache).unwrap(),
            1
        );
        // A newly staged file has no durability authority from a content hit.
        assert_eq!(
            finish(&mut verifier(root.path(), &bytes, true), &mut cache).unwrap(),
            2
        );
        cache.clear();
        assert_eq!(
            finish(&mut verifier(root.path(), &bytes, false), &mut cache).unwrap(),
            2
        );
    }
    #[test]
    fn same_length_corruption_with_restored_mtime_and_replacement_are_rejected() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("data");
        let mut cache = BTreeMap::new();
        fs::write(&path, b"original").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        finish(&mut verifier(root.path(), b"original", true), &mut cache).unwrap();
        let before = fs::metadata(&path).unwrap().modified().unwrap();
        fs::write(&path, b"modified").unwrap();
        File::open(&path)
            .unwrap()
            .set_times(FileTimes::new().set_modified(before))
            .unwrap();
        assert!(finish(&mut verifier(root.path(), b"original", false), &mut cache).is_err());
        fs::write(root.path().join("replacement"), b"replaced").unwrap();
        fs::rename(root.path().join("replacement"), &path).unwrap();
        assert!(finish(&mut verifier(root.path(), b"original", false), &mut cache).is_err());
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink("/dev/null", &path).unwrap();
        assert!(finish(&mut verifier(root.path(), b"original", false), &mut cache).is_err());
    }
    #[test]
    fn modification_to_an_already_read_chunk_invalidates_inflight_verification() {
        use std::io::{Seek, SeekFrom};
        let root = tempfile::tempdir().unwrap();
        let bytes = vec![42; PER_TICK + 1];
        let path = root.path().join("data");
        fs::write(&path, &bytes).unwrap();
        let mut cache = BTreeMap::new();
        let mut v = verifier(root.path(), &bytes, true);
        assert!(!v.advance(&mut cache, &mut |_| Ok(())).unwrap());
        let mut file = OpenOptions::new().write(true).open(&path).unwrap();
        file.seek(SeekFrom::Start(0)).unwrap();
        file.write_all(b"!").unwrap();
        assert!(finish(&mut v, &mut cache).is_err());
        assert!(cache.is_empty());
    }
}
