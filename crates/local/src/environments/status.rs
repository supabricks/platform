//! Read-only presentation data. Inspection never imports Python or resolves packages.
use super::*;
use crate::notebooks::files::directory::Directory;
use std::ffi::OsStr;

#[derive(Serialize)]
pub(super) struct Declaration {
    pub inputs: Option<Inputs>,
    state: &'static str,
    requirements: Vec<String>,
    error: Option<String>,
}
pub(super) fn declaration(worktree: &Path) -> Declaration {
    let mut result = Declaration {
        inputs: None,
        state: "absent",
        requirements: vec![],
        error: None,
    };
    let read = || -> Result<_> {
        let root = Directory::project(worktree)?;
        if !root.exists(OsStr::new("notebooks"))? {
            return Ok(None);
        }
        let notebooks = root.child(OsStr::new("notebooks"), false)?;
        if !notebooks.exists(OsStr::new("environment"))? {
            return Ok(None);
        }
        let dir = notebooks.child(OsStr::new("environment"), false)?;
        let manifest = files::document(&dir, "pyproject.toml")?;
        let lock = if dir.exists(OsStr::new("uv.lock"))? {
            Some(files::document(&dir, "uv.lock")?)
        } else {
            None
        };
        Ok(Some((manifest, lock)))
    };
    match read() {
        Ok(None) => (),
        Ok(Some((manifest, lock))) => {
            result.state = if lock.is_some() {
                "present"
            } else {
                "missing_lock"
            };
            result.inputs = lock.as_ref().map(|lock| Inputs {
                manifest: hash(&manifest),
                lock: hash(lock),
            });
            let parsed = std::str::from_utf8(&manifest)
                .ok()
                .and_then(|s| toml::from_str::<toml::Value>(s).ok());
            if let Some(value) = parsed {
                result.requirements = value
                    .get("project")
                    .and_then(|p| p.get("dependencies"))
                    .and_then(toml::Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(toml::Value::as_str)
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default();
            } else {
                result.state = "invalid";
                result.error = Some(
                    "The project pyproject.toml is invalid; fix the declaration before preparing."
                        .into(),
                );
            }
            if lock.is_none() {
                result.error = Some("uv.lock is missing. Restore the reviewed lock alongside pyproject.toml, then refresh. Existing notebook edits and kernels are retained.".into());
            }
        }
        Err(_) => {
            result.state = "invalid";
            result.error = Some("Cannot read the project environment declaration. Restore regular pyproject.toml and uv.lock files in notebooks/environment, then refresh.".into());
        }
    }
    result
}
pub(super) fn generations(
    store: &Store,
    binding: &Binding,
    package: &Package,
    operations: &[Operation],
) -> Result<Vec<Value>> {
    store.environment_generations()?.iter()
        .filter(|g| g.project == binding.project_id && g.worktree == binding.worktree && g.state == "ready")
        .map(|g| {
            let operation = operations.iter().find(|o| o.generation == Some(g.id) && o.state == "ready");
            let packages = operation.and_then(|o| o.result.as_ref()).and_then(|r| r.get("packages")).cloned()
                .or_else(|| operation.filter(|o| o.workflow.is_none() && o.contract == package.identity)
                    .and_then(|o| package.template(&o.template).ok()).map(|t| json!(t.packages)));
            Ok(json!({"id":g.id,"inputs":g.inputs,"packages":packages,"compatible":g.contract == package.identity && g.installation == package.installation,
                "python_version":if g.contract == package.identity {Some(&package.python_version)} else {None}}))
        }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inspection_distinguishes_missing_locks_and_never_follows_links() {
        let tmp = tempfile::tempdir().unwrap();
        let worktree = tmp.path();
        assert_eq!(declaration(worktree).state, "absent");
        let dir = worktree.join("notebooks/environment");
        fs::create_dir_all(&dir).unwrap();
        let manifest = "[project]\nname='demo'\nversion='0.1'\ndependencies=['humanize==4.13.0']\n";
        fs::write(dir.join("pyproject.toml"), manifest).unwrap();
        let missing = declaration(worktree);
        assert_eq!(missing.state, "missing_lock");
        assert!(missing.inputs.is_none());
        assert_eq!(missing.requirements, ["humanize==4.13.0"]);
        fs::write(dir.join("uv.lock"), "version = 1").unwrap();
        let present = declaration(worktree);
        assert_eq!(present.state, "present");
        assert_eq!(present.inputs.unwrap().manifest, hash(manifest.as_bytes()));
        fs::remove_file(dir.join("uv.lock")).unwrap();
        std::os::unix::fs::symlink(worktree.join("secret"), dir.join("uv.lock")).unwrap();
        assert_eq!(declaration(worktree).state, "invalid");
        assert!(!worktree.join("secret").exists());
        assert_eq!(
            fs::read_to_string(dir.join("pyproject.toml")).unwrap(),
            manifest
        );
    }
}
