//! Incremental file verification, durable generation promotion and owned GC.
//! SQLite is the only publication authority; directory presence never publishes.
use crate::store::{
    Publication, Result, Store,
    error::{conflict, invalid},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Component, Path, PathBuf},
};
use supabricks_core::resource::OperationId;

const MAX_MANIFEST: u64 = 2 * 1024 * 1024;
const CHUNK: usize = 1024 * 1024;
const PER_TICK: usize = 4 * CHUNK;
#[derive(Default)]
pub struct Publisher {
    verifier: Option<Verifier>,
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
    current: Option<(File, u64, Sha256)>,
}
struct FileCheck {
    path: String,
    bytes: u64,
    hash: String,
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
        })
    }
    fn advance(&mut self, hook: &mut impl FnMut(&str) -> Result<()>) -> Result<bool> {
        let mut budget = PER_TICK;
        let mut buffer = vec![0; CHUNK];
        while self.index < self.files.len() && budget > 0 {
            let check = &self.files[self.index];
            if self.current.is_none() {
                let f = OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_NOFOLLOW)
                    .open(self.root.join(&check.path))?;
                self.current = Some((f, 0, Sha256::new()));
            }
            let (file, read, digest) = self.current.as_mut().unwrap();
            let n = file.read(&mut buffer[..budget.min(CHUNK)])?;
            if n == 0 {
                require(
                    *read == check.bytes && hex::encode(digest.clone().finalize()) == check.hash,
                    "generation checksum mismatch",
                )?;
                file.sync_all()?;
                self.current = None;
                self.index += 1;
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
fn check_ready(root: &Path, d: &Value) -> Result<()> {
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
    layout(root, &d["manifest"])?;
    Ok(())
}
impl Publisher {
    pub fn recover(store: &mut Store) -> Result<Self> {
        let (stage, generations) = roots(store)?;
        let mut untracked = 0;
        for root in [&stage, &generations] {
            for entry in fs::read_dir(root)?.take(4097) {
                let name = entry?.file_name();
                let known = name
                    .to_str()
                    .and_then(|s| s.parse::<OperationId>().ok())
                    .is_some_and(|id| store.export(id).is_ok());
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
            let s = store.snapshot(project, id)?;
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
        self.tick_with_hook(store, &mut |_| Ok(()))
    }
    /// Hook is used by subprocess crash tests, never selected through public IPC.
    pub fn tick_with_hook(
        &mut self,
        store: &mut Store,
        hook: &mut impl FnMut(&str) -> Result<()>,
    ) -> Result<()> {
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
                if p.state == "requested" {
                    if self.verifier.is_none() {
                        self.verifier = Some(Verifier::open(
                            store,
                            p,
                            stage.join(p.export_id.to_string()),
                        )?);
                    }
                    let v = self.verifier.as_mut().unwrap();
                    if v.advance(hook)? {
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
                // If commit succeeded, an injected post-commit error cannot
                // undo publication or authorize deletion of the live epoch.
                if store.publication(p.export_id)?.state != "published" {
                    store.fail_publication(p.export_id, &e.to_string())?;
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
