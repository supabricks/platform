//! Pair publication uses one directory exchange, with the old pair retained.
//! The durable operation records both revisions before the exchange. Recovery
//! never activates an interrupted generation; an explicit sync reconstructs it.
use super::*;
use crate::notebooks::files::directory::Directory;
use std::{
    ffi::OsStr,
    io::{Read, Write},
    os::unix::fs::OpenOptionsExt,
};

pub(super) fn declaration(worktree: &Path, path: &Path) -> Result<(Vec<u8>, Vec<u8>)> {
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || (path != Path::new(".")
            && !path
                .components()
                .all(|p| matches!(p, std::path::Component::Normal(_))))
    {
        return Err(invalid(
            "declaration must name a project-contained relative directory",
        ));
    }
    let mut dir = Directory::project(worktree)?;
    for component in path
        .components()
        .filter(|p| !matches!(p, std::path::Component::CurDir))
    {
        dir = dir.child(component.as_os_str(), false)?;
    }
    Ok((
        files::document(&dir, "pyproject.toml")?,
        files::document(&dir, "uv.lock")?,
    ))
}
fn external(path: &Path) -> Result<()> {
    if !path.is_absolute()
        || path.file_name().is_none()
        || path.parent().unwrap().canonicalize()? != path.parent().unwrap()
    {
        return Err(invalid(
            "bundle path must have an absolute canonical parent",
        ));
    }
    Ok(())
}
pub(super) fn validate_change(worktree: &Path, change: &Change) -> Result<()> {
    match change {
        Change::Add { requirement }
        | Change::Remove {
            package: requirement,
        } => {
            if requirement.is_empty()
                || requirement.len() > 1024
                || requirement.starts_with('-')
                || requirement.contains(['\n', '\r', '\0', '@', '/', '\\'])
            {
                return Err(invalid(
                    "use one registry package requirement; paths, URLs and options are unsupported",
                ));
            }
        }
        Change::Adopt { path, .. } => {
            declaration(worktree, path)?;
        }
        Change::ExportBundle { path } | Change::ImportBundle { path } => external(path)?,
        _ => (),
    }
    Ok(())
}
fn write(dir: &Directory, name: &str, bytes: &[u8]) -> Result<()> {
    let mut f = dir.open(
        OsStr::new(name),
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
    )?;
    f.write_all(bytes)?;
    f.sync_all()?;
    Ok(())
}
pub(super) fn snapshot(o: &Operation, workflow: &Workflow, root: &Path) -> Result<()> {
    let path = Path::new("notebooks/environment");
    let (manifest, lock) = declaration(&o.worktree, path)?;
    if (Inputs {
        manifest: hash(&manifest),
        lock: hash(&lock),
    }) != o.inputs
    {
        return Err(conflict("environment inputs changed"));
    }
    let dir = Directory::project(root)?.child(OsStr::new("documents"), true)?;
    write(&dir, "pyproject.toml", &manifest)?;
    write(&dir, "uv.lock", &lock)?;
    if let Change::Adopt { path, expected } = &workflow.change {
        let (manifest, lock) = declaration(&o.worktree, path)?;
        if &(Inputs {
            manifest: hash(&manifest),
            lock: hash(&lock),
        }) != expected
        {
            return Err(conflict("adoption source changed"));
        }
        write(&dir, "adopt.toml", &manifest)?;
        write(&dir, "adopt.lock", &lock)?;
    }
    if let Change::ImportBundle { path } = &workflow.change {
        external(path)?;
        let source = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)?;
        let metadata = source.metadata()?;
        use std::os::unix::fs::MetadataExt;
        if !metadata.is_file() || metadata.nlink() != 1 || metadata.len() > 512 * 1024 * 1024 {
            return Err(invalid(
                "bundle must be a regular file no larger than 512 MiB",
            ));
        }
        let mut target = dir.open(
            OsStr::new("import.zip"),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
        )?;
        let count = std::io::copy(&mut source.take(512 * 1024 * 1024 + 1), &mut target)?;
        if count != metadata.len() {
            return Err(conflict("bundle changed or exceeded size limit"));
        }
        target.sync_all()?;
    }
    dir.sync()
}
pub(super) fn result_inputs(root: &Path) -> Result<Inputs> {
    let dir = Directory::project(root)?.child(OsStr::new("documents"), false)?;
    Ok(Inputs {
        manifest: hash(&files::document(&dir, "pyproject.toml")?),
        lock: hash(&files::document(&dir, "uv.lock")?),
    })
}
fn pair(dir: &Directory) -> Result<Inputs> {
    // Never move unrelated project files into a private historical revision.
    let entries = dir.entries(&mut 3)?;
    if entries.len() != 2
        || entries
            .iter()
            .any(|(n, _)| n != "pyproject.toml" && n != "uv.lock")
    {
        return Err(conflict(
            "managed declaration directory must contain only pyproject.toml and uv.lock",
        ));
    }
    Ok(Inputs {
        manifest: hash(&files::document(dir, "pyproject.toml")?),
        lock: hash(&files::document(dir, "uv.lock")?),
    })
}
pub(super) fn publish(o: &Operation, root: &Path) -> Result<()> {
    let expected = o
        .publication
        .as_ref()
        .ok_or_else(|| conflict("missing publication journal"))?;
    if let Some(Workflow {
        change: Change::Adopt { path, expected },
        ..
    }) = &o.workflow
    {
        let (manifest, lock) = declaration(&o.worktree, path)?;
        if &(Inputs {
            manifest: hash(&manifest),
            lock: hash(&lock),
        }) != expected
        {
            return Err(conflict("adoption source changed before publication"));
        }
    }
    let parent = Directory::project(&o.worktree)?.child(OsStr::new("notebooks"), false)?;
    let current = parent.child(OsStr::new("environment"), false)?;
    if pair(&current)? != o.inputs {
        return Err(conflict("environment changed before publication"));
    }
    if expected == &o.inputs {
        return Ok(());
    }
    let stage = format!(".environment-{}", o.id);
    if parent.exists(OsStr::new(&stage))? {
        return Err(conflict(
            "publication staging directory already exists; inspect retained files",
        ));
    }
    let target = parent.child(OsStr::new(&stage), true)?;
    let source = Directory::project(root)?.child(OsStr::new("documents"), false)?;
    for name in ["pyproject.toml", "uv.lock"] {
        write(&target, name, &files::document(&source, name)?)?;
    }
    target.sync()?;
    if pair(&target)? != *expected || pair(&current)? != o.inputs {
        return Err(conflict(
            "declarations changed during publication; both pairs retained",
        ));
    }
    parent.exchange(OsStr::new(&stage), OsStr::new("environment"))?;
    // An editor holding a descriptor across exchange can still edit the old
    // directory. Preserve that edit and fail closed instead of erasing it.
    if pair(&parent.child(OsStr::new(&stage), false)?)? != o.inputs
        || files::inputs(&o.worktree)? != *expected
    {
        return Err(conflict(
            "concurrent declaration edit; both revisions retained, inspect notebooks/.environment-<operation-id> before sync",
        ));
    }
    Ok(())
}

/// Publish a fully verified export without overwriting the destination.
pub(super) fn export(o: &Operation) -> Result<()> {
    if let Some(Workflow {
        change: Change::ExportBundle { path },
        ..
    }) = &o.workflow
    {
        external(path)?;
        let parent = Directory::project(path.parent().unwrap())?;
        let stage = format!(".supabricks-bundle-{}.tmp", o.id);
        parent.move_to(OsStr::new(&stage), &parent, path.file_name().unwrap())?;
    }
    Ok(())
}

/// Remove disposable transfer/build inputs only after the owned worker exits.
/// Keep declarations and diagnostic records for publication recovery/review.
pub(super) fn cleanup(root: &Path) -> Result<()> {
    let documents = root.join("documents");
    if !documents.exists() {
        return Ok(());
    }
    let dir = Directory::project(root)?.child(OsStr::new("documents"), false)?;
    for name in ["wheels", "qualified"] {
        let path = documents.join(name);
        if dir.exists(OsStr::new(name))? {
            // remove_dir_all does not follow a substituted symlink.
            fs::remove_dir_all(path)?;
        }
    }
    for name in ["download", "import.zip", "export.zip"] {
        if dir.exists(OsStr::new(name))? {
            dir.unlink(OsStr::new(name))?;
        }
    }
    dir.sync()
}
