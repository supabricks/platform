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
