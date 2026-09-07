//! Bounded supervised exporter; only completed, unpublished generations survive cleanup.
use super::*;
use crate::{
    operations::{Mutation, Status},
    store::ExportRecord,
};
use std::os::unix::fs::PermissionsExt;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkerConfig {
    python: PathBuf,
    worker: PathBuf,
}
impl Cell {
    pub(super) fn export_user(branch: &BranchRecord) -> String {
        format!(
            "sb_export_{}",
            branch.endpoint.id.to_string().replace('-', "")
        )
    }
    pub(crate) fn configure_exporter(
        &self,
        store: &Store,
        python: PathBuf,
        worker: PathBuf,
    ) -> Result<Value> {
        if !store.active_exports()?.is_empty() {
            return Err(conflict(
                "wait for export cleanup before changing the worker",
            ));
        }
        // Preserve the virtualenv interpreter symlink: resolving it would bypass pyvenv.cfg.
        if !python.is_absolute()
            || !worker.is_absolute()
            || !python.is_file()
            || !worker.is_file()
            || python.metadata()?.permissions().mode() & 0o111 == 0
        {
            return Err(invalid(
                "exporter requires absolute Python executable and worker script paths",
            ));
        }
        write_json(
            &self.root.join("analytics.json"),
            &WorkerConfig {
                python,
                worker: worker.canonicalize()?,
            },
        )?;
        Ok(json!({"configured":true,"published":false}))
    }
    fn exporter(&self) -> Result<WorkerConfig> {
        let data = fs::read(self.root.join("analytics.json"))
            .map_err(|_| invalid("configure the A01 worker with analytics configure first"))?;
        Ok(serde_json::from_slice(&data)?)
    }
    fn export_role(e: &ExportRecord) -> String {
        format!("export-{}", e.id)
    }
    fn stop_export_worker(&mut self, store: &mut Store, e: &ExportRecord) -> Result<()> {
        let role = Self::export_role(e);
        self.launches.remove(&role);
        if let Some(record) = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role)
        {
            supervisor::stop(&record)?;
            store.forget_native_process(&record)?;
        }
        if self.processes.remove(&role).is_some() {
            self.update()?;
        }
        Ok(())
    }
    pub(super) fn control_exports(&mut self, store: &mut Store) -> Result<()> {
        for mut e in store.active_exports()? {
            let now = chrono::Utc::now().timestamp_millis();
            if e.state != "cleaning" {
                if e.cancel_requested {
                    store.export_outcome(e.id, json!({"status":"cancelled","code":"cancelled"}))?;
                } else if now >= e.deadline_ms {
                    store.export_outcome(e.id, json!({"status":"failed","code":"deadline"}))?;
                }
                e = store.export(e.id)?;
            }
            if e.state == "cleaning" {
                self.stop_export_worker(store, &e)?;
            }
        }
        Ok(())
    }
    pub(super) fn tick_exports(&mut self, store: &mut Store) -> Result<()> {
        for mut e in store.active_exports()? {
            let workspace = self.root.join("export-work").join(e.id.to_string());
            let output = self.root.join("analytics/staging").join(e.id.to_string());
            if e.state == "cleaning" {
                self.stop_export_worker(store, &e)?;
                if e.cleanup_id.is_none() {
                    store.cleanup_export(&e)?;
                }
                e = store.export(e.id)?;
                if let Some(id) = e.cleanup_id
                    && store.operation(id)?.status == Status::Succeeded
                {
                    if e.outcome
                        .as_ref()
                        .is_none_or(|v| v["status"] != "files_complete")
                        && output.exists()
                    {
                        fs::remove_dir_all(&output)?;
                        fs::File::open(output.parent().unwrap())?.sync_all()?;
                    }
                    if workspace.exists() {
                        fs::remove_dir_all(&workspace)?;
                    }
                    store.finish_export(&e)?;
                }
                continue;
            }
            store.renew_export_lease(&e)?;
            let op = store.operation(e.id)?;
            if matches!(op.status, Status::Failed | Status::Superseded) {
                store
                    .export_outcome(e.id, json!({"status":"failed","code":"branch_preparation"}))?;
                continue;
            }
            if op.status != Status::Succeeded {
                continue;
            }
            let branch = store.branch(e.child_id)?;
            if e.state == "preparing" {
                if !self.connection_ready(store, &branch)? {
                    if self.ensure_timeline(store, &branch)? {
                        self.ensure_compute(store, &branch)?;
                    }
                    continue;
                }
                let worker = self.exporter()?;
                let source = store.branch(e.source_id)?;
                let source_identity = json!({"project_id":e.project_id,"branch_id":e.source_id,
                    "timeline_id":source.branch.timeline_id,"tenant_id":branch.branch.tenant_id,
                    "export_branch_id":e.child_id,"export_timeline_id":branch.branch.timeline_id,
                    "lsn":branch.branch.ancestor_lsn});
                dir(&workspace)?;
                dir(output.parent().unwrap())?;
                let input = workspace.join("input.json");
                write_json(
                    &input,
                    &json!({"id":e.id,"source":source_identity,
                    "port":branch.ports.ok_or_else(|| conflict("missing export ports"))?.sql,
                    "password":store.app_password(branch.endpoint.id)?,"username":Self::export_user(&branch),"output":output,
                    "report":workspace.join("result.json"),"deadline_ms":e.deadline_ms,"limits":e.limits}),
                )?;
                store.export_started(e.id)?;
                self.add(self.launch(
                    &Self::export_role(&e),
                    vec![
                        path(&worker.python)?,
                        "-W".into(),
                        "error".into(),
                        path(&worker.worker)?,
                        path(&input)?,
                    ],
                    Some((e.child_id, branch.revision)),
                    BTreeMap::new(),
                    workspace.clone(),
                ))?;
                self.update()?;
                continue;
            }
            let result_path = workspace.join("result.json");
            if result_path.is_file() {
                let result: Value = if result_path.metadata()?.len() > 65536 {
                    json!({"status":"failed","code":"oversized_worker_report"})
                } else {
                    serde_json::from_slice(&fs::read(&result_path)?).unwrap_or_else(
                        |_| json!({"status":"failed","code":"invalid_worker_report"}),
                    )
                };
                let source = store.branch(e.source_id)?;
                let valid = result["status"] == "files_complete"
                    && result["id"] == json!(e.id)
                    && result["published"] == false
                    && result["manifest"] == json!(output.join("manifest.json"))
                    && result["source"]["branch_id"] == json!(e.source_id)
                    && result["source"]["project_id"] == json!(e.project_id)
                    && result["source"]["timeline_id"] == json!(source.branch.timeline_id)
                    && result["source"]["tenant_id"] == json!(branch.branch.tenant_id)
                    && result["source"]["export_branch_id"] == json!(e.child_id)
                    && result["source"]["export_timeline_id"] == json!(branch.branch.timeline_id)
                    && result["source"]["lsn"] == json!(branch.branch.ancestor_lsn)
                    && result["bytes"]
                        .as_u64()
                        .is_some_and(|n| n <= e.limits.max_bytes)
                    && output.join("manifest.json").is_file();
                self.stop_export_worker(store, &e)?;
                store.export_outcome(
                    e.id,
                    if valid {
                        result
                    } else if result["status"] == "failed" {
                        result
                    } else {
                        json!({"status":"failed","code":"invalid_worker_report"})
                    },
                )?;
            } else if let Some(p) = store
                .native_processes()?
                .into_iter()
                .find(|p| p.role == Self::export_role(&e))
                && supervisor::os::identity(p.pid)?.is_none_or(|id| id.zombie)
            {
                store.export_outcome(e.id, json!({"status":"failed","code":"worker_exit"}))?;
            }
        }
        Ok(())
    }
    pub(super) fn validate_export(&self, mutation: &Mutation) -> Result<()> {
        if matches!(mutation, Mutation::Export { .. }) {
            self.exporter()?;
        }
        Ok(())
    }
}
