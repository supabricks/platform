//! Private staging and no-replace publication for project artifacts and exports.
use crate::{
    notebooks::files::directory::Directory,
    store::{Result, error::invalid},
};
use std::{ffi::OsStr, io::Write, path::Path};

pub(crate) struct Publication {
    parent: Directory,
    stage: Directory,
    temporary: tempfile::TempDir,
    name: std::ffi::OsString,
}
impl Publication {
    pub fn new(destination: &Path) -> Result<Self> {
        let name = destination
            .file_name()
            .filter(|n| *n != "." && *n != "..")
            .ok_or_else(|| invalid("destination requires a new file or directory name"))?
            .to_owned();
        let parent_path = destination
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."))
            .canonicalize()?;
        let parent = Directory::project(&parent_path)?;
        if parent.exists(&name)? {
            return Err(invalid("destination already exists; choose a new path"));
        }
        let temporary = tempfile::Builder::new()
            .prefix(".supabricks-package-")
            .tempdir_in(parent_path)?;
        let stage = Directory::project(temporary.path())?;
        use std::os::fd::AsRawFd;
        let handle = stage.open(OsStr::new("."), libc::O_RDONLY | libc::O_DIRECTORY)?;
        if unsafe { libc::fchmod(handle.as_raw_fd(), 0o700) } < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(Self {
            parent,
            stage,
            temporary,
            name,
        })
    }
    pub fn write(&self, name: &str, bytes: &[u8]) -> Result<()> {
        let parts = super::source::path(name, false)?;
        let mut directory = self.stage.child(OsStr::new("."), false)?;
        for part in &parts[..parts.len() - 1] {
            let child = directory.child(OsStr::new(part), true)?;
            directory.sync()?;
            directory = child;
            // Every extraction directory is private even when the caller has a permissive umask.
            use std::os::fd::AsRawFd;
            let f = directory.open(OsStr::new("."), libc::O_RDONLY | libc::O_DIRECTORY)?;
            if unsafe { libc::fchmod(f.as_raw_fd(), 0o700) } < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
        }
        let mut file = directory.open(
            OsStr::new(parts.last().unwrap()),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
        )?;
        file.write_all(bytes)?;
        file.sync_all()?;
        directory.sync()?;
        Ok(())
    }
    pub fn publish_file(self, name: &str) -> Result<()> {
        self.stage
            .move_to(OsStr::new(name), &self.parent, &self.name)
    }
    pub fn publish_directory(self) -> Result<()> {
        self.stage.sync()?;
        self.parent.move_to(
            self.temporary.path().file_name().unwrap(),
            &self.parent,
            &self.name,
        )
    }
    #[cfg(test)]
    pub fn staging_path(&self) -> std::path::PathBuf {
        self.temporary.path().to_owned()
    }
}
pub fn write_new(destination: &Path, bytes: &[u8]) -> Result<()> {
    let publication = Publication::new(destination)?;
    publication.write("payload", bytes)?;
    publication.publish_file("payload")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn interrupted_staging_is_private_and_never_visible_at_destination() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let dest = root.path().join("project");
        let p = Publication::new(&dest).unwrap();
        let temporary = p.staging_path();
        p.write("notebooks/deep/input.sql", b"SELECT 1").unwrap();
        assert_eq!(
            std::fs::metadata(&temporary).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert!(!dest.exists());
        drop(p);
        assert!(!dest.exists());
        assert!(!temporary.exists());
    }
    #[test]
    fn destination_created_during_staging_is_preserved() {
        let root = tempfile::tempdir().unwrap();
        let dest = root.path().join("project");
        let p = Publication::new(&dest).unwrap();
        p.write("supabricks.toml", b"new").unwrap();
        std::fs::create_dir(&dest).unwrap();
        std::fs::write(dest.join("original"), b"keep").unwrap();
        assert!(p.publish_directory().is_err());
        assert!(!dest.join("supabricks.toml").exists());
        assert_eq!(std::fs::read(dest.join("original")).unwrap(), b"keep");
    }
}
