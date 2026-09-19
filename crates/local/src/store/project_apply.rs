use super::*;
use crate::project_apply::{Operation, Resource};
use std::collections::BTreeMap;
impl Store {
    pub fn latest_deployment_apply(&self, deployment: DeploymentId) -> Result<Option<Operation>> {
        let value: Option<String> = self.db.query_row(
            "SELECT record_json FROM project_applies WHERE deployment_id=?1 ORDER BY rowid DESC LIMIT 1",
            [deployment.to_string()], |r| r.get(0),
        ).optional()?;
        value
            .map(|v| serde_json::from_str(&v).map_err(Into::into))
            .transpose()
    }

    pub fn deployment_resources(
        &self,
        deployment: DeploymentId,
    ) -> Result<BTreeMap<String, Resource>> {
        self.db.prepare("SELECT logical,record_json FROM deployment_resources WHERE deployment_id=?1 ORDER BY logical")?.query_map([deployment.to_string()],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.map(|r|{let(k,v)=r?;Ok((k,serde_json::from_str(&v)?))}).collect()
    }
    pub fn active_deployment(&self, deployment: DeploymentId) -> Result<Option<OperationId>> {
        let id: Option<String> = self
            .db
            .query_row(
                "SELECT revision_id FROM deployment_active WHERE deployment_id=?1",
                [deployment.to_string()],
                |r| r.get(0),
            )
            .optional()?;
        id.map(|v| parse(&v)).transpose()
    }
    pub fn project_apply(&self, deployment: DeploymentId, id: OperationId) -> Result<Operation> {
        let value: String = self
            .db
            .query_row(
                "SELECT record_json FROM project_applies WHERE deployment_id=?1 AND id=?2",
                params![deployment.to_string(), id.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("project apply in this deployment"))?;
        Ok(serde_json::from_str(&value)?)
    }
    pub fn find_apply(&self, deployment: DeploymentId, key: &str) -> Result<Option<Operation>> {
        let value: Option<String> = self
            .db
            .query_row(
                "SELECT record_json FROM project_applies WHERE deployment_id=?1 AND request_key=?2",
                params![deployment.to_string(), key],
                |r| r.get(0),
            )
            .optional()?;
        value
            .map(|v| serde_json::from_str(&v).map_err(Into::into))
            .transpose()
    }
    pub(crate) fn pending_applies(&self) -> Result<Vec<Operation>> {
        self.db.prepare("SELECT record_json FROM project_applies WHERE state IN ('queued','preparing','activating') ORDER BY rowid")?.query_map([],|r|r.get::<_,String>(0))?.map(|v|Ok(serde_json::from_str(&v?)?)).collect()
    }
    pub(crate) fn insert_apply(&self, o: &Operation) -> Result<()> {
        if self
            .pending_applies()?
            .iter()
            .any(|p| p.plan.context.deployment_id == o.plan.context.deployment_id)
        {
            return Err(conflict(
                "deployment has a pending apply; inspect or cancel it first",
            ));
        }
        self.db.execute(
            "INSERT INTO project_applies VALUES (?1,?2,?3,?4,?5)",
            params![
                o.id.to_string(),
                o.plan.context.deployment_id.to_string(),
                o.key,
                o.state,
                serde_json::to_string(o)?
            ],
        )?;
        Ok(())
    }
    pub(crate) fn save_apply(&self, o: &Operation) -> Result<()> {
        self.db.execute(
            "UPDATE project_applies SET state=?2,record_json=?3 WHERE id=?1",
            params![o.id.to_string(), o.state, serde_json::to_string(o)?],
        )?;
        Ok(())
    }
    // Allocation ownership is durable even if the enclosing apply is later cancelled.
    pub(crate) fn own_database(
        &self,
        deployment: DeploymentId,
        logical: &str,
        r: &Resource,
    ) -> Result<()> {
        let previous = self.deployment_resources(deployment)?.remove(logical);
        if previous.as_ref().is_some_and(|p| p.branch != r.branch) {
            return Err(conflict("logical database already owns another branch"));
        }
        if previous.is_none() {
            self.db.execute(
                "INSERT INTO deployment_resources VALUES (?1,?2,?3)",
                params![deployment.to_string(), logical, serde_json::to_string(r)?],
            )?;
        }
        Ok(())
    }
    pub(crate) fn activate_deployment(&mut self, o: &mut Operation) -> Result<()> {
        let tx = self.db.transaction()?;
        let ctx = &o.plan.context;
        if tx.execute(
            "UPDATE deployments SET revision=revision+1 WHERE id=?1 AND revision=?2",
            params![ctx.deployment_id.to_string(), ctx.revision],
        )? != 1
        {
            return Err(conflict("deployment revision changed before activation"));
        }
        for (logical, r) in &o.resources {
            tx.execute("INSERT INTO deployment_resources VALUES (?1,?2,?3) ON CONFLICT(deployment_id,logical) DO UPDATE SET record_json=excluded.record_json",params![ctx.deployment_id.to_string(),logical,serde_json::to_string(r)?])?;
        }
        tx.execute(
            "INSERT INTO deployment_revisions VALUES (?1,?2,?3)",
            params![
                o.id.to_string(),
                ctx.deployment_id.to_string(),
                serde_json::to_string(&o.resources)?
            ],
        )?;
        tx.execute("INSERT INTO deployment_active VALUES (?1,?2) ON CONFLICT(deployment_id) DO UPDATE SET revision_id=excluded.revision_id",params![ctx.deployment_id.to_string(),o.id.to_string()])?;
        o.state = "succeeded".into();
        tx.execute(
            "UPDATE project_applies SET state=?2,record_json=?3 WHERE id=?1",
            params![o.id.to_string(), o.state, serde_json::to_string(o)?],
        )?;
        crate::project_apply::checkpoint("activation_transaction");
        tx.commit()?;
        Ok(())
    }
}
