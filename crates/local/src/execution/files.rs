use super::*;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
};

pub(crate) struct Input {
    pub root: PathBuf,
    pub table: String,
    pub files: Vec<Value>,
}
pub(crate) struct Prepared {
    pub dir: tempfile::TempDir,
}
fn relative(value: &str) -> Result<()> {
    if value.is_empty()
        || value.len() > 256
        || value.contains(['\\', '%', ':'])
        || value
            .split('/')
            .any(|s| s.is_empty() || s == "." || s == "..")
        || !Path::new(value)
            .components()
            .all(|v| matches!(v, Component::Normal(_)))
    {
        return Err(denied());
    }
    Ok(())
}
// Walk each component with O_NOFOLLOW. A directory symlink cannot redirect an
// otherwise checksum-valid copy into the cell's credentials or another tenant.
fn open(root: &Path, path: &str) -> Result<fs::File> {
    use std::os::fd::{AsRawFd, FromRawFd};
    relative(path)?;
    if root.canonicalize()? != root {
        return Err(denied());
    }
    let mut directory = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY | libc::O_CLOEXEC)
        .open(root)?;
    let parts: Vec<_> = path.split('/').collect();
    for (i, part) in parts.iter().enumerate() {
        let name = std::ffi::CString::new(*part).map_err(|_| denied())?;
        let flags = libc::O_RDONLY
            | libc::O_NOFOLLOW
            | libc::O_CLOEXEC
            | libc::O_NONBLOCK
            | if i + 1 < parts.len() {
                libc::O_DIRECTORY
            } else {
                0
            };
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        directory = unsafe { fs::File::from_raw_fd(fd) };
    }
    if !directory.metadata()?.is_file() {
        return Err(denied());
    }
    Ok(directory)
}
pub(crate) fn verify_file(root: &Path, path: &str, bytes: Option<u64>, hash: &str) -> Result<()> {
    let mut file = open(root, path)?;
    if let Some(bytes) = bytes
        && file.metadata()?.len() != bytes
    {
        return Err(denied());
    }
    let mut sha = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        sha.update(&buf[..n]);
    }
    if hex::encode(sha.finalize()) != hash {
        return Err(denied());
    }
    Ok(())
}
impl Prepared {
    pub(crate) fn build(
        root: &Path,
        kind: &str,
        contents: &str,
        inputs: Vec<Input>,
    ) -> Result<Self> {
        let work = root.join("isolated-work");
        match fs::DirBuilder::new().mode(0o700).create(&work) {
            Ok(()) => (),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e.into()),
        }
        crate::catalog::config::directory(&work)?;
        let dir = tempfile::Builder::new().prefix("lease-").tempdir_in(work)?;
        let mut total = 0u64;
        let mut count = 0;
        for input in inputs {
            uuid(&input.table)?;
            let dest = dir.path().join("data").join(&input.table);
            fs::create_dir_all(&dest)?;
            let mut names = BTreeSet::new();
            for entry in input.files {
                count += 1;
                if count > 4096 {
                    return Err(denied());
                }
                let from = entry["path"].as_str().ok_or_else(denied)?;
                relative(from)?;
                let (_, name) = from.split_once('/').ok_or_else(denied)?;
                relative(name)?;
                if !names.insert(name.to_owned()) {
                    return Err(denied());
                }
                let size = entry["bytes"].as_u64().ok_or_else(denied)?;
                total = total.checked_add(size).ok_or_else(denied)?;
                if total > MAX_BYTES {
                    return Err(denied());
                }
                let mut file = open(&input.root, from)?;
                if file.metadata()?.len() != size {
                    return Err(denied());
                }
                let target = dest.join(name);
                fs::create_dir_all(target.parent().unwrap())?;
                let mut out = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o444)
                    .open(&target)?;
                let mut sha = Sha256::new();
                let mut buf = [0u8; 65536];
                let mut actual = 0;
                loop {
                    let n = file.read(&mut buf)?;
                    if n == 0 {
                        break;
                    }
                    actual += n as u64;
                    if actual > size {
                        return Err(denied());
                    }
                    sha.update(&buf[..n]);
                    out.write_all(&buf[..n])?;
                }
                if actual != size || entry["sha256"] != hex::encode(sha.finalize()) {
                    return Err(denied());
                }
                out.sync_all()?;
            }
            validate_delta(&dest, &names)?;
        }
        fs::create_dir_all(dir.path().join("data"))?;
        fs::write(
            dir.path().join("source.json"),
            serde_json::to_vec(&json!({"kind":kind,"contents":contents}))?,
        )?;
        fs::write(
            dir.path().join("supervisor.py"),
            include_str!("../../../../python/execution/supervisor.py"),
        )?;
        fs::write(
            dir.path().join("workload.py"),
            include_str!("../../../../python/execution/workload.py"),
        )?;
        // The outer mount is read-only; no workload can alter these files. Only
        // UUID-named admitted data, never the producer generation, is mounted.
        // The control-plane account need not have guest UID 1000. Explicit
        // readability also works under an operator's restrictive umask; the
        // containing isolated-work directory remains host-private (0700).
        let mut directories = vec![dir.path().to_owned()];
        while let Some(directory) = directories.pop() {
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o755))?;
            for entry in fs::read_dir(directory)? {
                let entry = entry?;
                if entry.file_type()?.is_dir() {
                    directories.push(entry.path());
                } else {
                    fs::set_permissions(entry.path(), fs::Permissions::from_mode(0o444))?;
                }
            }
        }
        Ok(Self { dir })
    }
}
fn validate_delta(root: &Path, names: &BTreeSet<String>) -> Result<()> {
    let log = "_delta_log/00000000000000000000.json";
    let bytes = fs::read(root.join(log))?;
    if bytes.len() > 2 * 1024 * 1024 {
        return Err(denied());
    }
    let mut expected = BTreeSet::from([log.to_owned()]);
    let mut metadata = 0;
    let mut protocol = 0;
    for line in bytes.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
        let v: Value = serde_json::from_slice(line)?;
        if v.as_object().is_none_or(|m| m.len() != 1) {
            return Err(denied());
        }
        if let Some(add) = v.get("add") {
            let path = add["path"].as_str().ok_or_else(denied)?;
            relative(path)?;
            if !path.ends_with(".parquet")
                || add.get("deletionVector").is_some()
                || !expected.insert(path.into())
            {
                return Err(denied());
            }
            if fs::metadata(root.join(path))?.len() != add["size"].as_u64().ok_or_else(denied)? {
                return Err(denied());
            }
        } else if let Some(m) = v.get("metaData") {
            metadata += 1;
            if m["format"]["provider"] != "parquet"
                || m["partitionColumns"]
                    .as_array()
                    .is_none_or(|a| !a.is_empty())
                || m["configuration"]
                    .as_object()
                    .is_some_and(|a| !a.is_empty())
            {
                return Err(denied());
            }
        } else if let Some(p) = v.get("protocol") {
            protocol += 1;
            if p["minReaderVersion"] != 1 || p.get("readerFeatures").is_some() {
                return Err(denied());
            }
        } else if v.get("commitInfo").is_none() {
            return Err(denied());
        }
    }
    if protocol != 1 || metadata != 1 || &expected != names {
        return Err(denied());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (tempfile::TempDir, Input) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("producer");
        fs::create_dir_all(root.join("42/_delta_log")).unwrap();
        let log=[json!({"protocol":{"minReaderVersion":1,"minWriterVersion":2}}),json!({"metaData":{"format":{"provider":"parquet"},"partitionColumns":[],"configuration":{}}}),json!({"add":{"path":"data.parquet","size":4}})].iter().map(Value::to_string).collect::<Vec<_>>().join("\n");
        fs::write(root.join("42/data.parquet"), b"test").unwrap();
        fs::write(root.join("42/_delta_log/00000000000000000000.json"), log).unwrap();
        let files = ["42/data.parquet", "42/_delta_log/00000000000000000000.json"]
            .iter()
            .map(|p| {
                let bytes = fs::read(root.join(p)).unwrap();
                json!({"path":p,"bytes":bytes.len(),"sha256":hex::encode(Sha256::digest(&bytes))})
            })
            .collect();
        (
            dir,
            Input {
                root,
                table: uuid::Uuid::new_v4().to_string(),
                files,
            },
        )
    }
    #[test]
    fn exact_file_closure_is_copied_and_escape_variants_are_denied() {
        let (root, input) = fixture();
        let table = input.table.clone();
        let source = input.root.clone();
        let ready = Prepared::build(root.path(), "sql", "select 1", vec![input]).unwrap();
        assert_eq!(
            fs::read(ready.dir.path().join(format!("data/{table}/data.parquet"))).unwrap(),
            b"test"
        );
        fs::write(source.join("42/data.parquet"), b"evil").unwrap();
        assert_eq!(
            fs::read(ready.dir.path().join(format!("data/{table}/data.parquet"))).unwrap(),
            b"test"
        );
        for path in [
            "../secret",
            "/etc/passwd",
            "a/../x",
            "a//x",
            "a/%2e%2e/x",
            "file:x",
            "a\\x",
            "",
        ] {
            assert!(relative(path).is_err(), "{path}");
        }
        let (root, input) = fixture();
        fs::remove_file(input.root.join("42/data.parquet")).unwrap();
        std::os::unix::fs::symlink("/etc/passwd", input.root.join("42/data.parquet")).unwrap();
        assert!(Prepared::build(root.path(), "sql", "", vec![input]).is_err());
        let (root, input) = fixture();
        fs::rename(input.root.join("42"), input.root.join("43")).unwrap();
        std::os::unix::fs::symlink("43", input.root.join("42")).unwrap();
        assert!(Prepared::build(root.path(), "sql", "", vec![input]).is_err());
        let (root, mut input) = fixture();
        input.files[0]["sha256"] = json!("0".repeat(64));
        assert!(Prepared::build(root.path(), "sql", "", vec![input]).is_err());
        let (root, mut input) = fixture();
        input.files[0]["bytes"] = json!(MAX_BYTES + 1);
        assert!(Prepared::build(root.path(), "sql", "", vec![input]).is_err());
    }
    #[test]
    fn delta_log_cannot_name_unadmitted_files_or_versions() {
        for path in [
            "../private.parquet",
            "/secret.parquet",
            "%2e%2e/secret.parquet",
            "missing.parquet",
        ] {
            let (root, mut input) = fixture();
            let p = input.root.join("42/_delta_log/00000000000000000000.json");
            let s = fs::read_to_string(&p)
                .unwrap()
                .replace("data.parquet", path);
            fs::write(&p, &s).unwrap();
            input.files[1]["bytes"] = json!(s.len());
            input.files[1]["sha256"] = json!(hex::encode(Sha256::digest(s.as_bytes())));
            assert!(Prepared::build(root.path(), "sql", "", vec![input]).is_err());
        }
        let (root, mut input) = fixture();
        input.files.push(input.files[0].clone());
        assert!(Prepared::build(root.path(), "sql", "", vec![input]).is_err());
    }
}
