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
fn document(dir: &Directory, name: &str) -> Result<Vec<u8>> {
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
