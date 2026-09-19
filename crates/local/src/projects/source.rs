//! Bounded descriptor-relative source reads. No symlinks, execution or writes.
use crate::{
    notebooks::files::directory::Directory,
    store::{
        Result,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap, ffi::OsStr, fs::Metadata, io::Read, os::unix::fs::MetadataExt,
    path::Path,
};

pub const MAX_FILE: u64 = 8 * 1024 * 1024;
pub const MAX_TOTAL: u64 = 288 * 1024 * 1024;
pub const MAX_BUNDLE: u64 = 128 * 1024 * 1024;
pub fn is_bundle(path: &str) -> bool {
    path.starts_with("dependencies/") && path.ends_with(".zip")
}
pub fn file_limit(path: &str) -> u64 {
    if is_bundle(path) {
        MAX_BUNDLE
    } else if path.ends_with(".toml") || path.ends_with(".lock") {
        1024 * 1024
    } else {
        MAX_FILE
    }
}
pub const MAX_FILES: usize = 1024;
pub const MAX_ENTRIES: usize = 4096;
pub const MAX_DEPTH: usize = 16;
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub bytes: u64,
    pub sha256: String,
}
pub struct Source {
    root: Option<Directory>,
    memory: Option<BTreeMap<String, Vec<u8>>>,
    pub payload: BTreeMap<String, Vec<u8>>,
    pub files: BTreeMap<String, Entry>,
    stamps: BTreeMap<String, Metadata>,
    folded: BTreeMap<String, String>,
    total: u64,
    budget: usize,
}
pub fn path(value: &str, pattern: bool) -> Result<Vec<&str>> {
    let parts: Vec<_> = value.split('/').collect();
    if value.len() > 512
        || parts.len() > MAX_DEPTH
        || parts.iter().any(|p| {
            p.is_empty()
                || *p == "."
                || *p == ".."
                || p.ends_with(' ')
                || p.ends_with('.')
                || !p.bytes().all(|b| {
                    b.is_ascii_alphanumeric() || b"_.- ".contains(&b) || (pattern && b == b'*')
                })
                || (p.contains('*') && *p != "**" && p.matches('*').count() > 1)
        })
        || parts.iter().filter(|p| **p == "**").count() > 1
    {
        return Err(invalid(
            "expected a portable project-relative path; patterns support * within a segment and one ** directory segment",
        ));
    }
    if parts.iter().any(|p| {
        matches!(
            p.to_ascii_lowercase().as_str(),
            ".git"
                | ".supabricks"
                | ".env"
                | ".venv"
                | "venv"
                | "node_modules"
                | "__pycache__"
                | ".ipynb_checkpoints"
                | ".ssh"
                | ".aws"
                | ".kube"
                | ".pypirc"
                | ".npmrc"
                | ".config"
                | "postgres"
                | "pageserver"
                | "safekeeper"
                | "storage_broker"
                | "supabricks"
                | "credentials"
                | "connections.json"
                | "runtime.json"
                | "state.sqlite3"
                | "id_rsa"
                | "id_ed25519"
        ) || p.to_ascii_lowercase().starts_with(".env.")
            || p.to_ascii_lowercase().ends_with(".pem")
            || p.to_ascii_lowercase().ends_with(".key")
    }) {
        return Err(invalid(
            "private configuration, key, cache or environment paths cannot be project inputs",
        ));
    }
    Ok(parts)
}
fn same(a: &Metadata, b: &Metadata) -> bool {
    a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.len() == b.len()
        && a.mtime() == b.mtime()
        && a.mtime_nsec() == b.mtime_nsec()
        && a.ctime() == b.ctime()
        && a.ctime_nsec() == b.ctime_nsec()
}
impl Source {
    pub fn new(root: &Path) -> Result<Self> {
        Ok(Self {
            root: Some(Directory::project(root)?),
            memory: None,
            payload: BTreeMap::new(),
            files: BTreeMap::new(),
            stamps: BTreeMap::new(),
            folded: BTreeMap::new(),
            total: 0,
            budget: MAX_ENTRIES,
        })
    }
    pub fn memory(files: BTreeMap<String, Vec<u8>>) -> Result<Self> {
        if files.len() > MAX_FILES
            || files.values().map(|b| b.len() as u64).sum::<u64>() > MAX_TOTAL
        {
            return Err(invalid("package inventory exceeds source limits"));
        }
        let mut source = Self {
            root: None,
            memory: Some(files),
            payload: BTreeMap::new(),
            files: BTreeMap::new(),
            stamps: BTreeMap::new(),
            folded: BTreeMap::new(),
            total: 0,
            budget: MAX_ENTRIES,
        };
        // Validate even unselected paths; inspection subsequently rejects extra payloads.
        let names: Vec<_> = source.memory.as_ref().unwrap().keys().cloned().collect();
        for name in names {
            source.record_path(&name)?;
            let parts: Vec<_> = name.split('/').collect();
            for count in 1..parts.len() {
                if source
                    .memory
                    .as_ref()
                    .unwrap()
                    .contains_key(&parts[..count].join("/"))
                {
                    return Err(invalid("package file is also used as a directory"));
                }
            }
        }
        Ok(source)
    }
    fn record_path(&mut self, value: &str) -> Result<()> {
        path(value, false)?;
        let mut prefix = String::new();
        for part in value.split('/') {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(part);
            if self
                .folded
                .insert(prefix.to_ascii_lowercase(), prefix.clone())
                .is_some_and(|old| old != prefix)
            {
                return Err(invalid("case-colliding project file or directory paths"));
            }
        }
        Ok(())
    }
    fn open(&self, value: &str) -> Result<std::fs::File> {
        let parts = path(value, false)?;
        let mut dir = self.root.as_ref().unwrap().child(OsStr::new("."), false)?;
        for p in &parts[..parts.len() - 1] {
            dir = dir.child(OsStr::new(p), false)?;
        }
        dir.open(OsStr::new(parts[parts.len() - 1]), libc::O_RDONLY)
            .map_err(|e| invalid(format!("cannot read declared project file {value}: {e}")))
    }
    pub fn read(&mut self, value: &str) -> Result<Vec<u8>> {
        if let Some(memory) = &self.memory {
            path(value, false)?;
            let bytes = memory
                .get(value)
                .ok_or_else(|| invalid(format!("missing declared project file: {value}")))?
                .clone();
            let limit = file_limit(value);
            if bytes.len() as u64 > limit {
                return Err(invalid("package file exceeds source limit"));
            }
            self.files.insert(
                value.into(),
                Entry {
                    bytes: bytes.len() as u64,
                    sha256: hex::encode(Sha256::digest(&bytes)),
                },
            );
            return Ok(bytes);
        }
        let file = self.open(value)?;
        let before = file.metadata()?;
        let limit = file_limit(value);
        if !before.is_file() || before.nlink() != 1 || before.len() > limit {
            return Err(invalid(format!(
                "{value}: expected bounded regular file (limit {limit} bytes)"
            )));
        }
        let fresh = !self.files.contains_key(value);
        if fresh && (self.files.len() >= MAX_FILES || self.total + before.len() > MAX_TOTAL) {
            return Err(invalid(
                "project input inventory exceeds 1024 files or 288 MiB",
            ));
        }
        let mut bytes = Vec::new();
        (&file).take(limit + 1).read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        if bytes.len() as u64 > limit || !same(&before, &after) {
            return Err(conflict("project input changed during inspection; retry"));
        }
        let sha256 = hex::encode(Sha256::digest(&bytes));
        if let Some(prior) = self.files.get(value) {
            if prior.sha256 != sha256 || !same(&self.stamps[value], &after) {
                return Err(conflict("project input changed during inspection; retry"));
            }
        } else {
            self.record_path(value)?;
            self.total += after.len();
            self.files.insert(
                value.into(),
                Entry {
                    sha256,
                    bytes: after.len(),
                },
            );
            self.stamps.insert(value.into(), after);
        }
        self.payload.insert(value.into(), bytes.clone());
        Ok(bytes)
    }
    pub fn verify(&self) -> Result<()> {
        let (mut ordinary, mut bundles) = (0, 0);
        for (path, entry) in &self.files {
            if is_bundle(path) {
                bundles += entry.bytes;
            } else {
                ordinary += entry.bytes;
            }
        }
        if ordinary > 32 * 1024 * 1024 || bundles > 256 * 1024 * 1024 {
            return Err(invalid(
                "project exceeds 32 MiB source or 256 MiB wheel-bundle budget",
            ));
        }
        for (name, stamp) in &self.stamps {
            if !same(stamp, &self.open(name)?.metadata()?) {
                return Err(conflict(
                    "project input changed or was replaced during inspection; retry",
                ));
            }
        }
        Ok(())
    }
    pub fn expand(&mut self, pattern: &str) -> Result<Vec<String>> {
        let parts = path(pattern, true)?;
        if let Some(memory) = &self.memory {
            let names: Vec<_> = memory
                .keys()
                .filter(|name| glob(&parts, &name.split('/').collect::<Vec<_>>()))
                .cloned()
                .collect();
            if names.is_empty() {
                return Err(invalid(format!(
                    "package include matched no files: {pattern}"
                )));
            }
            for name in &names {
                self.read(name)?;
            }
            return Ok(names);
        }
        let dir = self.root.as_ref().unwrap().child(OsStr::new("."), false)?;
        let mut found = Vec::new();
        self.walk(&dir, &parts, "", 0, &mut found)?;
        found.sort();
        found.dedup();
        if found.is_empty() {
            return Err(invalid(format!(
                "package include matched no files: {pattern}"
            )));
        }
        Ok(found)
    }
    fn walk(
        &mut self,
        dir: &Directory,
        parts: &[&str],
        prefix: &str,
        depth: usize,
        out: &mut Vec<String>,
    ) -> Result<()> {
        if depth > MAX_DEPTH {
            return Err(invalid("project include exceeds directory depth limit"));
        }
        if parts.is_empty() {
            return Ok(());
        }
        let segment = parts[0];
        if !segment.contains('*') {
            let name = format!("{prefix}{segment}");
            if parts.len() == 1 {
                self.read(&name)?;
                out.push(name);
            } else {
                let child = dir.child(OsStr::new(segment), false)?;
                self.walk(&child, &parts[1..], &format!("{name}/"), depth + 1, out)?;
            }
            return Ok(());
        }
        if segment == "**" && parts.len() > 1 {
            self.walk(dir, &parts[1..], prefix, depth, out)?;
        }
        let mut entries = dir.portable_entries(&mut self.budget)?;
        entries.sort();
        for (entry, kind) in entries {
            let matches = segment == "**"
                || segment.split_once('*').is_some_and(|(a, b)| {
                    entry.len() >= a.len() + b.len() && entry.starts_with(a) && entry.ends_with(b)
                });
            if !matches {
                continue;
            }
            let name = format!("{prefix}{entry}");
            path(&name, false)?;
            if kind == libc::S_IFDIR as u32 {
                if segment == "**" || parts.len() > 1 {
                    let child = dir.child(OsStr::new(&entry), false)?;
                    let remaining = if segment == "**" { parts } else { &parts[1..] };
                    self.walk(&child, remaining, &format!("{name}/"), depth + 1, out)?;
                }
            } else if kind != libc::S_IFREG as u32 {
                return Err(invalid("project include matched a special file"));
            } else if parts.len() == 1 {
                self.read(&name)?;
                out.push(name);
            }
        }
        Ok(())
    }
}

fn glob(pattern: &[&str], path: &[&str]) -> bool {
    match (pattern.first(), path.first()) {
        (None, None) => true,
        (Some(&"**"), _) => {
            glob(&pattern[1..], path) || (!path.is_empty() && glob(pattern, &path[1..]))
        }
        (Some(p), Some(n)) => {
            let matches = p.split_once('*').map_or(p == n, |(a, b)| {
                n.len() >= a.len() + b.len() && n.starts_with(a) && n.ends_with(b)
            });
            matches && glob(&pattern[1..], &path[1..])
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn replacement_after_read_is_detected_and_never_followed() {
        let t = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("a.sql"), "SELECT 1").unwrap();
        let mut source = Source::new(t.path()).unwrap();
        source.read("a.sql").unwrap();
        std::fs::rename(t.path().join("a.sql"), t.path().join("old.sql")).unwrap();
        std::fs::write(t.path().join("a.sql"), "SELECT 1").unwrap();
        assert!(source.verify().is_err());
    }
    #[test]
    fn directory_case_collisions_are_rejected_without_host_fs_assumptions() {
        let t = tempfile::tempdir().unwrap();
        let mut source = Source::new(t.path()).unwrap();
        source.folded.insert("query".into(), "Query".into());
        std::fs::create_dir(t.path().join("query")).unwrap();
        std::fs::write(t.path().join("query/a.sql"), "SELECT 1").unwrap();
        assert!(source.read("query/a.sql").is_err());
    }
}
