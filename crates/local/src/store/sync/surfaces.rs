//! Read-only discovery and reviewed generation replacement shared by all adapters.
use super::*;

impl Store {
    pub(crate) fn inspect_sync(
        &self,
        project: ProjectId,
        deployment: DeploymentId,
        branch: &str,
    ) -> Result<Value> {
        let id = crate::api::resolve(
            self,
            &crate::api::Binding {
                project_id: project,
                worktree: self.root().to_owned(),
            },
            Some(branch),
        )?;
        let b = self.branch_in_project(project, id)?;
        let governed = self.governed_branch(id)?;
        Ok(
            json!({"branch_id":id,"deployment_id":deployment,"branch_revision":b.revision,
            "source_available":!b.expired && b.endpoint.desired_state==DesiredState::Running,
            "governed":governed,"profile":"whole_public_tables_integer_primary_key",
            "source_qualification":"not_probed",
            "requirements":["Logged public tables with one integer primary key", "Integer, text/varchar and bounded numeric columns", "No RLS, schema changes or unsupported application objects", "One capture per installation; retained WAL and spool budgets apply"],
            "enrollment":"explicit_create_only","continuous_keeps_compute_awake":true,
            "event_triggers":false,"reverse_sync":false}),
        )
    }

    pub(crate) fn review_sync_resync(&self, project: ProjectId, id: OperationId) -> Result<Value> {
        let p = self.sync_policy(project, id)?;
        if !p.config.incremental() || p.state == "deleted" {
            return Err(conflict("resync review requires an incremental policy"));
        }
        let c = p
            .capture_id
            .map(|id| self.capture(project, id))
            .transpose()?;
        let mut review = json!({"policy_id":id,"expected_revision":p.revision,
            "capture_id":p.capture_id,"capture_identity":c.as_ref().map(|c|&c.identity),
            "source_timeline":p.timeline_id,
            "effect":"Cancel active work, pause the policy and retire its capture. Published epochs and existing readers remain pinned. After cleanup, explicitly resume to create a new capture and full bootstrap.",
            "requires_full_bootstrap":true,"deletes_published_epochs":false});
        review["review_hash"] = json!(crate::project_apply::digest(&review)?);
        Ok(review)
    }
}
