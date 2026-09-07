//! A release is immutable and discovered relative to the executable, never cwd.
use crate::store::{Result, error::invalid};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::PermissionsExt,
    path::{Component, Path, PathBuf},
};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct File {
    pub sha256: String,
    pub executable: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub format_version: u32,
    pub version: String,
    pub target: String,
    pub profile: String,
    pub provenance: serde_json::Value,
    pub files: BTreeMap<String, File>,
}

pub struct Installation {
    pub root: PathBuf,
    pub identity: String,
    pub manifest: Manifest,
}

impl Installation {
    pub fn discover() -> Result<Option<Self>> {
        Self::at_executable(&std::env::current_exe()?.canonicalize()?)
    }

    fn at_executable(exe: &Path) -> Result<Option<Self>> {
        let Some(root) = exe.parent().and_then(Path::parent) else {
            return Ok(None);
        };
        let path = root.join("release.json");
        if !path.try_exists()? {
            return Ok(None);
        }
        let bytes = fs::read(path)?;
        let manifest: Manifest = serde_json::from_slice(&bytes)?;
        let target = if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
            "linux-x86_64"
        } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
            "macos-arm64"
        } else {
            "unsupported"
        };
        if manifest.format_version != 1
            || manifest.profile != "local-postgres-alpha"
            || manifest.target != target
            || !manifest.files.contains_key("bin/supabricks")
        {
            return Err(invalid(
                "invalid or incompatible Supabricks release manifest",
            ));
        }
        Ok(Some(Self {
            root: root.to_owned(),
            identity: hex::encode(Sha256::digest(&bytes)),
            manifest,
        }))
    }

    pub fn bundle(&self) -> PathBuf {
        self.root.join("engine")
    }

    pub fn helpers(&self) -> PathBuf {
        self.root.join("helpers")
    }

    /// Used before installer activation and before starting a packaged daemon.
    /// Publication authenticity is established by the bootstrap signature.
    pub fn verify(&self) -> Result<()> {
        let mut actual = BTreeMap::new();
        inventory(&self.root, &self.root, &mut actual)?;
        actual.remove("release.json");
        if actual.keys().ne(self.manifest.files.keys()) {
            return Err(invalid(
                "installed file inventory differs from release manifest",
            ));
        }
        for (name, expected) in &self.manifest.files {
            if !Path::new(name)
                .components()
                .all(|c| matches!(c, Component::Normal(_)))
            {
                return Err(invalid("invalid release path"));
            }
            let (path, executable) = &actual[name];
            let mut file = fs::File::open(path)?;
            let mut hash = Sha256::new();
            let mut buffer = [0; 64 * 1024];
            loop {
                let n = file.read(&mut buffer)?;
                if n == 0 {
                    break;
                }
                hash.update(&buffer[..n]);
            }
            if hex::encode(hash.finalize()) != expected.sha256 || *executable != expected.executable
            {
                return Err(invalid(format!(
                    "installed file checksum/mode mismatch: {name}"
                )));
            }
        }
        Ok(())
    }
}

fn inventory(
    root: &Path,
    directory: &Path,
    files: &mut BTreeMap<String, (PathBuf, bool)>,
) -> Result<()> {
    for entry in fs::read_dir(directory)? {
        let path = entry?.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.is_dir() {
            inventory(root, &path, files)?;
        } else if metadata.is_file() {
            let name = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            files.insert(name, (path, metadata.permissions().mode() & 0o111 != 0));
        } else {
            return Err(invalid(
                "installed release contains a symlink or special file",
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_integrity_rejects_mutations_extra_files_and_symlinks() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("bin")).unwrap();
        let exe = root.path().join("bin/supabricks");
        fs::write(&exe, b"binary").unwrap();
        let manifest = Manifest {
            format_version: 1,
            version: "v0.1.0-alpha.1".into(),
            target: if cfg!(target_os = "macos") {
                "macos-arm64"
            } else {
                "linux-x86_64"
            }
            .into(),
            profile: "local-postgres-alpha".into(),
            provenance: serde_json::json!({}),
            files: BTreeMap::from([(
                "bin/supabricks".into(),
                File {
                    sha256: hex::encode(Sha256::digest(b"binary")),
                    executable: false,
                },
            )]),
        };
        fs::write(
            root.path().join("release.json"),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        let installed = Installation::at_executable(&exe).unwrap().unwrap();
        installed.verify().unwrap();
        fs::write(&exe, b"changed").unwrap();
        assert!(installed.verify().is_err());
        fs::write(&exe, b"binary").unwrap();
        fs::write(root.path().join("extra"), b"unexpected").unwrap();
        assert!(installed.verify().is_err());
        fs::remove_file(root.path().join("extra")).unwrap();
        fs::remove_file(&exe).unwrap();
        std::os::unix::fs::symlink("/bin/sh", &exe).unwrap();
        assert!(installed.verify().is_err());
    }
}
