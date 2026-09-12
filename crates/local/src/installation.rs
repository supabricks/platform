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

    pub(crate) fn at_executable(exe: &Path) -> Result<Option<Self>> {
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
            || !matches!(
                manifest.profile.as_str(),
                "local-postgres-alpha" | "local-analytical-preview"
            )
            || manifest.target != target
            || !manifest.files.contains_key("bin/supabricks")
        {
            return Err(invalid(
                "invalid or incompatible Supabricks release manifest",
            ));
        }
        if manifest.profile == "local-analytical-preview"
            && [
                "python/analytics/python",
                "python/runtime/bin/python3.12",
                "python/analytics/export.py",
                "python/analytics/session.py",
                "python/analytics/shell.py",
            ]
            .iter()
            .any(|name| !manifest.files.contains_key(*name))
        {
            return Err(invalid(
                "analytical preview manifest is missing its private worker",
            ));
        }
        if let Some(notebooks) = manifest.provenance.get("notebooks") {
            if notebooks["protocol_version"] != crate::notebooks::contract::PROTOCOL
                || [
                    "python/notebooks/server.py",
                    "python/notebooks/kernel.py",
                    "python/notebooks/bootstrap.py",
                    "python/notebooks/uv.lock",
                    "python/notebooks/requirements.lock",
                ]
                .iter()
                .any(|name| !manifest.files.contains_key(*name))
                || notebooks["uv_lock_sha256"].as_str()
                    != manifest
                        .files
                        .get("python/notebooks/uv.lock")
                        .map(|f| f.sha256.as_str())
            {
                return Err(invalid(
                    "notebook manifest is missing its qualified worker inventory",
                ));
            }
        }
        if let Some(environments) = manifest.provenance.get("environments")
            && (environments["version"] != 1
                || !manifest.files.contains_key("helpers/uv")
                || !manifest
                    .files
                    .contains_key("python/notebooks/environment-worker.py")
                || environments["contract_sha256"].as_str()
                    != manifest
                        .files
                        .get("python/notebooks/kernel-contract.json")
                        .map(|f| f.sha256.as_str()))
        {
            return Err(invalid(
                "environment manifest is missing its qualified component",
            ));
        }
        if let Some(ingestion) = manifest.provenance.get("ingestion") {
            if ingestion["protocol_version"] != crate::ingest::VERSION
                || !manifest.files.contains_key("python/ingest/worker.py")
                || ingestion["worker_sha256"].as_str()
                    != manifest
                        .files
                        .get("python/ingest/worker.py")
                        .map(|f| f.sha256.as_str())
            {
                return Err(invalid(
                    "ingestion release manifest is missing its compatible worker inventory",
                ));
            }
        }
        if let Some(console) = manifest.provenance.get("console") {
            if console["api_version"] != crate::console::assets::VERSION
                || !manifest.files.contains_key("share/console/console.json")
                || !manifest.files.contains_key("share/console/index.html")
            {
                return Err(invalid(
                    "console release manifest is missing its compatible asset inventory",
                ));
            }
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

    pub fn analytical_worker(&self) -> Option<(PathBuf, PathBuf)> {
        (self.manifest.profile == "local-analytical-preview").then(|| {
            (
                self.root.join("python/analytics/python"),
                self.root.join("python/analytics/export.py"),
            )
        })
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

/// Installed workers are bound to this release and discovered after relocation.
/// Source builds and the explicitly smaller PG profile retain developer setup.
pub fn analytical_worker(root: &Path) -> Result<(PathBuf, PathBuf)> {
    let configured = Installation::discover()?.and_then(|i| i.analytical_worker());
    let (python, worker) = match configured {
        Some(paths) => paths,
        None => {
            let value: serde_json::Value = serde_json::from_slice(&fs::read(root.join("analytics.json"))
                .map_err(|_| invalid("install the analytical preview or run analytics configure with the locked Python environment"))?)?;
            let field = |name| {
                value[name]
                    .as_str()
                    .map(PathBuf::from)
                    .ok_or_else(|| invalid("invalid analytical worker configuration"))
            };
            (field("python")?, field("worker")?)
        }
    };
    if !python.is_absolute()
        || !worker.is_absolute()
        || !python.is_file()
        || !worker.is_file()
        || python.metadata()?.permissions().mode() & 0o111 == 0
    {
        return Err(invalid("analytical Python executable or worker is missing"));
    }
    Ok((python, worker))
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
    fn analytical_profile_requires_worker_inventory_and_resolves_from_release() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("bin")).unwrap();
        let target = if cfg!(target_os = "macos") {
            "macos-arm64"
        } else {
            "linux-x86_64"
        };
        let mut manifest = serde_json::json!({"format_version":1,"version":"v0.1.0-alpha.2",
            "target":target,"profile":"local-analytical-preview","provenance":{},
            "files":{"bin/supabricks":{"sha256":"unused","executable":true}}});
        let path = root.path().join("release.json");
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        let exe = root.path().join("bin/supabricks");
        assert!(Installation::at_executable(&exe).is_err());
        for name in [
            "python/analytics/python",
            "python/runtime/bin/python3.12",
            "python/analytics/export.py",
            "python/analytics/session.py",
            "python/analytics/shell.py",
        ] {
            manifest["files"][name] = serde_json::json!({"sha256":"unused","executable":true});
        }
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        let installed = Installation::at_executable(&exe).unwrap().unwrap();
        assert_eq!(
            installed.analytical_worker(),
            Some((
                root.path().join("python/analytics/python"),
                root.path().join("python/analytics/export.py")
            ))
        );
        manifest["provenance"]["ingestion"] =
            serde_json::json!({"protocol_version":1,"worker_sha256":"csv-worker"});
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert!(Installation::at_executable(&exe).is_err());
        manifest["files"]["python/ingest/worker.py"] =
            serde_json::json!({"sha256":"wrong-worker","executable":false});
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert!(Installation::at_executable(&exe).is_err());
        manifest["files"]["python/ingest/worker.py"]["sha256"] = serde_json::json!("csv-worker");
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert!(Installation::at_executable(&exe).is_ok());
        manifest["provenance"]["console"] = serde_json::json!({"api_version":1});
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert!(Installation::at_executable(&exe).is_err());
        for name in ["share/console/console.json", "share/console/index.html"] {
            manifest["files"][name] = serde_json::json!({"sha256":"unused","executable":false});
        }
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert!(Installation::at_executable(&exe).is_ok());
        manifest["provenance"]["console"]["api_version"] = serde_json::json!(2);
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert!(Installation::at_executable(&exe).is_err());
    }

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

/// Fail before opening SQLite, incrementing ownership or launching recovery.
/// Release changes require the explicit, backed-up upgrade transaction.
pub(crate) fn check_data_root(root: &Path) -> Result<()> {
    if root.join("restore-incomplete").exists() {
        return Err(crate::store::error::conflict(
            "incomplete restore; restore the verified backup into another new directory",
        ));
    }
    if root.join("upgrade.json").exists() {
        return Err(crate::store::error::conflict(
            "interrupted upgrade; rerun the staged installer with the same upgrade and backup options",
        ));
    }
    let path = root.join("runtime.json");
    if path.exists() {
        let runtime: serde_json::Value = serde_json::from_slice(&fs::read(path)?)?;
        if !matches!(runtime["version"].as_u64(), Some(1 | 2)) {
            return Err(invalid("unsupported native runtime format"));
        }
        if let Some(expected) = runtime["installation_identity"].as_str() {
            let installed = Installation::discover()?
                .ok_or_else(|| invalid("this data root requires its installed release"))?;
            if installed.identity != expected {
                return Err(crate::store::error::conflict(
                    "release mismatch; use the explicit backup and upgrade workflow before opening this data root",
                ));
            }
        }
    }
    Ok(())
}
