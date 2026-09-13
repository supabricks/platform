use super::*;
use crate::notebooks::files::directory::Directory;
use std::{
    ffi::OsStr,
    fs::{File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
};

pub(super) fn directory(path: &Path) -> Result<(u64, u64)> {
    if !path.exists() {
        fs::DirBuilder::new().mode(0o700).create(path)?;
        File::open(path.parent().unwrap())?.sync_all()?;
    }
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } || m.mode() & 0o077 != 0 {
        return Err(conflict(
            "environment directory must be private, owned and without symlinks",
        ));
    }
    Ok((m.dev(), m.ino()))
}
pub(super) fn generation_path(store: &Store, g: &Generation, create: bool) -> Result<PathBuf> {
    let parent = store.root().join("notebook-environments");
    let project = parent.join(&g.worktree_key);
    for p in [&parent, &project] {
        if !create && !p.exists() {
            return Err(conflict("environment directory is missing; prepare again"));
        }
        directory(p)?;
    }
    let path = project.join(g.id.to_string());
    if create {
        directory(&path)?;
    }
    let m = fs::symlink_metadata(&path)?;
    if !m.is_dir()
        || m.uid() != unsafe { libc::geteuid() }
        || m.mode() & 0o077 != 0
        || (m.dev(), m.ino()) != g.directory
        || path != g.path
    {
        return Err(conflict(
            "environment directory identity changed; prepare again",
        ));
    }
    Ok(path)
}
pub(super) fn deletion_finished(store: &Store, g: &Generation) -> Result<bool> {
    let parent = store
        .root()
        .join("notebook-environments")
        .join(&g.worktree_key);
    if parent.join(g.id.to_string()) != g.path {
        return Err(conflict("invalid generation path"));
    }
    let directory = Directory::project(store.root())?
        .child(OsStr::new("notebook-environments"), false)?
        .child(OsStr::new(&g.worktree_key), false)?;
    if directory.exists(OsStr::new(&g.id.to_string()))? {
        return Ok(false);
    }
    directory.sync()?;
    Ok(true)
}
pub(super) fn read(path: &Path, limit: u64) -> Result<Vec<u8>> {
    let f = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let m = f.metadata()?;
    if !m.is_file() || m.nlink() != 1 || m.len() > limit {
        return Err(conflict("invalid bounded environment file"));
    }
    let mut bytes = Vec::new();
    f.take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(conflict("environment file exceeds limit"));
    }
    Ok(bytes)
}
pub(super) fn document(dir: &Directory, name: &str) -> Result<Vec<u8>> {
    let f = dir.open(OsStr::new(name), libc::O_RDONLY)?;
    let m = f.metadata()?;
    if !m.is_file() || m.nlink() != 1 || m.len() > 1024 * 1024 {
        return Err(conflict("invalid environment declaration"));
    }
    let mut bytes = Vec::new();
    f.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err(conflict("environment declaration exceeds limit"));
    }
    Ok(bytes)
}
pub(super) fn inputs(worktree: &Path) -> Result<Inputs> {
    // Open each component relative to a directory capability; never follow a
    // notebook/environment/file symlink, including substitutions during reads.
    let dir = Directory::project(worktree)?
        .child(OsStr::new("notebooks"), false)?
        .child(OsStr::new("environment"), false)?;
    let manifest = document(&dir, "pyproject.toml")?;
    let lock = document(&dir, "uv.lock")?;
    Ok(Inputs {
        manifest: hash(&manifest),
        lock: hash(&lock),
    })
}
pub(super) fn initialize(o: &Operation, template: &Template, package: &Package) -> Result<()> {
    let parent = Directory::project(&o.worktree)?.child(OsStr::new("notebooks"), true)?;
    if parent.exists(OsStr::new("environment"))? {
        if inputs(&o.worktree)? != o.inputs {
            return Err(conflict(
                "environment declarations already exist with different content",
            ));
        }
        return Ok(());
    }
    let stage = format!(".environment-{}", o.id);
    let stage = OsStr::new(&stage);
    // A durable operation owns this unique staging directory. Retrying fills
    // only missing identical files. Existing project declarations are untouched.
    let dir = parent.child(stage, true)?;
    for (name, relative, want) in [
        ("pyproject.toml", &template.manifest, &o.inputs.manifest),
        ("uv.lock", &template.lock, &o.inputs.lock),
    ] {
        let bytes = package.bytes(relative)?;
        if &hash(&bytes) != want {
            return Err(conflict("qualified environment template changed"));
        }
        if dir.exists(OsStr::new(name))? {
            if hash(&document(&dir, name)?) != *want {
                return Err(conflict("environment staging file changed"));
            }
        } else {
            let mut f = dir.open(
                OsStr::new(name),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            )?;
            f.write_all(&bytes)?;
            f.sync_all()?;
        }
    }
    dir.sync()?;
    parent.move_to(stage, &parent, OsStr::new("environment"))?;
    if inputs(&o.worktree)? != o.inputs {
        return Err(conflict(
            "environment declarations changed during initialization",
        ));
    }
    Ok(())
}
#[allow(clippy::unnecessary_cast)] // statvfs integer widths differ on macOS.
pub(super) fn free_bytes(path: &Path) -> Result<u64> {
    use std::os::unix::ffi::OsStrExt;
    let p = std::ffi::CString::new(path.as_os_str().as_bytes())
        .map_err(|_| invalid("invalid data path"))?;
    let mut value = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(p.as_ptr(), value.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let value = unsafe { value.assume_init() };
    Ok((value.f_bavail as u64).saturating_mul(value.f_frsize as u64))
}

/// Match the worker's canonical inventory. No symlink is dereferenced, and
/// bounded reads reject hard links, special files, deep trees and excess bytes.
pub(super) fn inventory(root: &Path) -> Result<String> {
    fn visit(
        root: &Path,
        path: &Path,
        depth: usize,
        values: &mut BTreeMap<String, Value>,
        bytes: &mut u64,
    ) -> Result<()> {
        if depth > 64 {
            return Err(conflict("environment directory depth limit"));
        }
        for entry in fs::read_dir(path)? {
            let path = entry?.path();
            let m = fs::symlink_metadata(&path)?;
            let name = path
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .ok_or_else(|| conflict("invalid environment filename"))?
                .to_owned();
            if m.is_dir() {
                visit(root, &path, depth + 1, values, bytes)?;
            } else {
                if values.len() >= 100000 {
                    return Err(conflict("environment file count limit"));
                }
                let value = if m.file_type().is_symlink() {
                    json!({"link":fs::read_link(&path)?})
                } else {
                    let mut f = OpenOptions::new()
                        .read(true)
                        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                        .open(&path)?;
                    let m = f.metadata()?;
                    *bytes = bytes.saturating_add(m.len());
                    if !m.is_file() || m.nlink() != 1 || *bytes > 1024 * 1024 * 1024 {
                        return Err(conflict("environment inventory exceeds its contract"));
                    }
                    let mut digest = Sha256::new();
                    let mut buffer = [0; 64 * 1024];
                    let mut remaining = m.len();
                    loop {
                        let n = f.read(&mut buffer)?;
                        if n == 0 {
                            break;
                        }
                        if n as u64 > remaining {
                            return Err(conflict("environment file changed during verification"));
                        }
                        remaining -= n as u64;
                        digest.update(&buffer[..n]);
                    }
                    if remaining != 0 {
                        return Err(conflict("environment file changed during verification"));
                    }
                    json!(hex::encode(digest.finalize()))
                };
                values.insert(name, value);
            }
        }
        Ok(())
    }
    let mut values = BTreeMap::new();
    visit(root, root, 0, &mut values, &mut 0)?;
    Ok(hash(&serde_json::to_vec(&values)?))
}

/// Bound resolver scratch/cache bytes while the owned subprocess is running.
/// This scans metadata only, never hashes or follows symlinks.
pub(super) fn bounded_size(root: &Path, limit: u64) -> Result<()> {
    fn walk(
        path: &Path,
        bytes: &mut u64,
        entries: &mut usize,
        depth: usize,
        limit: u64,
    ) -> Result<()> {
        if depth > 64 {
            return Err(conflict("environment scratch depth limit"));
        }
        let listing = match fs::read_dir(path) {
            Ok(v) => v,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.into()),
        };
        for entry in listing {
            let path = entry?.path();
            let m = match fs::symlink_metadata(&path) {
                Ok(v) => v,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(e) => return Err(e.into()),
            };
            *entries += 1;
            if *entries > 100_000 {
                return Err(conflict("environment scratch entry limit"));
            }
            if m.is_dir() {
                walk(&path, bytes, entries, depth + 1, limit)?;
            } else {
                *bytes = bytes.saturating_add(m.len());
            }
            if *bytes > limit {
                return Err(conflict("environment scratch or cache size limit"));
            }
        }
        Ok(())
    }
    walk(root, &mut 0, &mut 0, 0, limit)
}
