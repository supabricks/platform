//! Sail 0.7.1 does not decode Delta object-store URL prefixes. A session-owned
//! ASCII alias avoids interpreting local spaces, percent signs or Unicode as
//! different filesystem names. Only the alias is removed, never its target.
use crate::store::{Result, error::conflict};
use std::{
    fs,
    os::unix::fs::{MetadataExt, symlink},
    path::{Path, PathBuf},
};
use supabricks_core::resource::OperationId;

fn alias(id: OperationId) -> PathBuf {
    Path::new("/tmp").join(format!("supabricks-sail-{}-{id}", unsafe {
        libc::geteuid()
    }))
}

pub(super) fn create(workspace: &Path, id: OperationId) -> Result<Option<PathBuf>> {
    if workspace
        .as_os_str()
        .as_encoded_bytes()
        .iter()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'/' | b'_' | b'-' | b'.'))
    {
        return Ok(None);
    }
    let path = alias(id);
    // Exclusive creation: never overwrite or follow a pre-existing /tmp entry.
    symlink(workspace, &path)?;
    Ok(Some(path))
}

pub(super) fn remove(workspace: &Path, id: OperationId) -> Result<()> {
    let path = alias(id);
    let metadata = match fs::symlink_metadata(&path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    if !metadata.is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || fs::read_link(&path)? != workspace
    {
        return Err(conflict(
            "analytical path alias changed; inspect the retained session",
        ));
    }
    // /tmp's sticky ownership and the unpredictable durable session ID scope
    // this entry. unlink never traverses even a substituted symlink target.
    fs::remove_file(path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alias_is_exclusive_and_cleanup_never_traverses_its_target() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("space % ' é");
        fs::create_dir(&workspace).unwrap();
        fs::write(workspace.join("keep"), "data").unwrap();
        let id = OperationId::new();
        let path = create(&workspace, id).unwrap().unwrap();
        assert!(path.as_os_str().as_encoded_bytes().is_ascii());
        assert_eq!(fs::read_link(&path).unwrap(), workspace);
        assert!(create(&workspace, id).is_err());
        remove(&workspace, id).unwrap();
        remove(&workspace, id).unwrap();
        assert_eq!(fs::read_to_string(workspace.join("keep")).unwrap(), "data");
    }

    #[test]
    fn recovery_rejects_a_replaced_alias_and_leaves_unrelated_entries() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("with spaces");
        let id = OperationId::new();
        let path = create(&workspace, id).unwrap().unwrap();
        fs::remove_file(&path).unwrap();
        symlink(root.path(), &path).unwrap();
        assert!(remove(&workspace, id).is_err());
        assert!(fs::symlink_metadata(&path).unwrap().is_symlink());
        fs::remove_file(&path).unwrap();
        fs::write(&path, "unrelated").unwrap();
        assert!(remove(&workspace, id).is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), "unrelated");
        fs::remove_file(&path).unwrap();
    }
}
