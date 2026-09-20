//! The lock file is permanent: unlinking it could create two lock owners.
use super::error::{Result, conflict, invalid};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
};

pub(crate) struct DataRoot {
    pub path: PathBuf,
    _lock: File,
}
impl Drop for DataRoot {
    fn drop(&mut self) {
        // Closing only our descriptor can leave the lock held by a concurrent
        // spawn until its inherited CLOEXEC descriptor closes. End ownership
        // explicitly so a stopped backup/reopen can acquire the root at once.
        let _ = self._lock.unlock();
    }
}
impl DataRoot {
    pub fn acquire(path: &Path) -> Result<Self> {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)?;
        let path = path.canonicalize()?;
        let meta = fs::metadata(&path)?;
        // SAFETY: geteuid has no pointer arguments or failure case.
        if meta.uid() != unsafe { libc::geteuid() } || meta.permissions().mode() & 0o777 != 0o700 {
            return Err(invalid(
                "data root must be owned by this user with mode 0700",
            ));
        }
        let lock = private_file(&path.join("owner.lock"))?;
        lock.try_lock().map_err(|e| match e {
            fs::TryLockError::WouldBlock => conflict("another daemon owns this data root"),
            fs::TryLockError::Error(e) => e.into(),
        })?;
        File::open(&path)?.sync_all()?;
        Ok(Self { path, _lock: lock })
    }
}
pub(crate) fn private_file(path: &Path) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let meta = file.metadata()?;
    // SAFETY: geteuid has no pointer arguments or failure case.
    if !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0
    {
        return Err(invalid(
            "state files must be private regular files without hard links",
        ));
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owner_release_is_not_delayed_by_an_inherited_descriptor() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("data");
        let owner = DataRoot::acquire(&path).unwrap();
        // A concurrent spawn can inherit this open file description until exec
        // closes CLOEXEC descriptors. Model that window without forking Rust.
        let inherited = owner._lock.try_clone().unwrap();
        assert!(DataRoot::acquire(&path).is_err());
        drop(owner);
        let next = DataRoot::acquire(&path).unwrap();
        drop(inherited);
        assert!(DataRoot::acquire(&path).is_err());
        drop(next);
        assert!(DataRoot::acquire(&path).is_ok());
    }
}
