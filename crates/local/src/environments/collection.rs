//! Journal deletion on the catalog owner, then remove files off its control loop.
use super::*;

impl Manager {
    pub(crate) fn collection_job(
        &self,
        store: &mut Store,
        binding: &Binding,
    ) -> Result<crate::store::IdentityJob> {
        binding.validate(store)?;
        if binding.worktree.canonicalize()? != binding.worktree {
            return Err(conflict("environment worktree must be canonical"));
        }
        self.collect_job(store, Some((binding.project_id, &binding.worktree)))
    }

    fn collect_job(
        &self,
        store: &mut Store,
        scope: Option<(ProjectId, &Path)>,
    ) -> Result<crate::store::IdentityJob> {
        let mut selected = Vec::new();
        for mut g in store.environment_generations()? {
            if scope.is_some_and(|(p, w)| p != g.project || w != g.worktree)
                || !["ready", "invalid", "deleting"].contains(&g.state.as_str())
            {
                continue;
            }
            if g.state == "deleting" && files::deletion_finished(store, &g).unwrap_or(false) {
                selected.push((g, false));
                continue;
            }
            // The transaction excludes active pointers and live leases. Once
            // deleting is durable, neither selection nor acquisition admits g.
            if files::generation_path(store, &g, false).is_err() {
                continue;
            }
            g.state = "deleting".into();
            if store.collect_environment(&g)? {
                selected.push((g, true));
            }
        }
        let root = store.root().to_path_buf();
        Ok(Box::new(move || {
            for (g, remove) in &selected {
                if *remove {
                    // Revalidate identity on the worker immediately before IO.
                    let path = files::generation_path_at(&root, g, false)?;
                    fs::remove_dir_all(&path)?;
                    fs::File::open(path.parent().unwrap())?.sync_all()?;
                }
            }
            Ok(Box::new(move |store: &mut Store| {
                let mut collected = Vec::new();
                for (mut g, _) in selected {
                    // A failed worker or daemon leaves deleting records, which
                    // the next explicit collection resumes after crash recovery.
                    if !files::deletion_finished(store, &g)? {
                        return Err(conflict("environment deletion is incomplete"));
                    }
                    g.state = "removed".into();
                    store.save_environment_generation(&g)?;
                    collected.push(g.id);
                }
                Ok(json!({"collected":collected}))
            }))
        }))
    }

    // Direct callers retain synchronous semantics; daemon requests use the job
    // so slow tree removal cannot starve notebook ownership heartbeats.
    pub(super) fn collect(
        &self,
        store: &mut Store,
        scope: Option<(ProjectId, &Path)>,
    ) -> Result<Vec<OperationId>> {
        let commit = self.collect_job(store, scope)?()?;
        Ok(serde_json::from_value(commit(store)?["collected"].clone())?)
    }
}
