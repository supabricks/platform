use super::*;

/// Portable provenance contains no local executable path or launch credentials.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Identity {
    pub id: OperationId,
    pub inputs: Inputs,
    pub contract: String,
    pub inventory: String,
}
pub struct Selected {
    pub identity: Identity,
    pub python: PathBuf,
}
pub enum DefaultPreparation {
    Ready(Selected),
    Pending(OperationId),
}
impl Manager {
    pub fn select(store: &mut Store, binding: &Binding, id: OperationId) -> Result<Selected> {
        binding.validate(store)?;
        if binding.worktree.canonicalize()? != binding.worktree {
            return Err(conflict("environment worktree must be canonical"));
        }
        let g = store
            .environment_generations()?
            .into_iter()
            .find(|g| {
                g.id == id
                    && g.project == binding.project_id
                    && g.worktree == binding.worktree
                    && g.state == "ready"
            })
            .ok_or_else(|| {
                missing("ready environment in this worktree; prepare and adopt explicitly")
            })?;
        let package = Package::load(store)?;
        package.verify_executable("python/runtime/bin/python3.12")?;
        let path = files::generation_path(store, &g, false)?;
        if g.contract != package.identity
            || g.installation != package.installation
            || g.interpreter != package.root.join("python/runtime/bin/python3.12")
        {
            return Err(conflict(
                "environment interpreter or contract changed; prepare and adopt explicitly",
            ));
        }
        let inventory = files::inventory(&path)?;
        if g.inventory.as_ref() != Some(&inventory) {
            return Err(conflict(
                "environment files drifted; prepare and adopt explicitly",
            ));
        }
        files::generation_path(store, &g, false)?;
        Ok(Selected {
            identity: Identity {
                id,
                inputs: g.inputs,
                contract: g.contract,
                inventory,
            },
            python: path.join("bin/python"),
        })
    }
    pub fn declarations_changed(store: &Store, binding: &Binding, identity: &Identity) -> bool {
        files::inputs(&binding.worktree).ok().as_ref() != Some(&identity.inputs)
            || Package::load(store).is_ok_and(|p| p.identity != identity.contract)
    }
    /// Called only after explicit notebook start, never while merely opening a file.
    pub fn prepare_default(
        &mut self,
        store: &mut Store,
        binding: &Binding,
        key: &str,
        previous: Option<OperationId>,
    ) -> Result<DefaultPreparation> {
        if let Some(id) = previous {
            let status = self.handle(store, binding, Command::Status { id })?;
            match status["state"].as_str() {
                Some("ready") => (),
                Some("failed" | "cancelled") => {
                    return Err(conflict(
                        "default environment preparation failed; inspect env operation and retry start explicitly",
                    ));
                }
                _ => return Ok(DefaultPreparation::Pending(id)),
            }
        }
        let status = self.handle(store, binding, Command::Inspect)?;
        if status["preparation_needed"] == false {
            let id = serde_json::from_value(status["active_generation"].clone())?;
            return Ok(DefaultPreparation::Ready(Self::select(store, binding, id)?));
        }
        let command = if status["inputs"].is_null() {
            Command::Initialize {
                key: format!("{key}:init"),
                template: "base".into(),
            }
        } else {
            Command::Prepare {
                key: format!("{key}:prepare"),
                expected: serde_json::from_value(status["inputs"].clone())?,
            }
        };
        // Another notebook in this worktree may already be preparing its default.
        if let Some(operation) = store.environment_operations()?.into_iter().find(|o| {
            o.project == binding.project_id
                && o.worktree == binding.worktree
                && ACTIVE.contains(&o.state.as_str())
        }) {
            return Ok(DefaultPreparation::Pending(operation.id));
        }
        let operation = self.handle(store, binding, command)?;
        Ok(DefaultPreparation::Pending(serde_json::from_value(
            operation["id"].clone(),
        )?))
    }
}
