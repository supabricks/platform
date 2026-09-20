use super::*;
use crate::{
    catalog::metadata::{Asset, Namespace},
    deployments::Context,
};
impl Store {
    pub fn catalog_namespace(
        &self,
        owner: DeploymentId,
        provider: &str,
    ) -> Result<Option<Namespace>> {
        let record:Option<String>=self.db.query_row("SELECT record_json FROM catalog_namespaces WHERE deployment_id=?1 AND provider_id=?2",params![owner.to_string(),provider],|r|r.get(0)).optional()?;
        record
            .map(|v| serde_json::from_str(&v).map_err(Into::into))
            .transpose()
    }
    pub(crate) fn save_catalog_namespace(&mut self, n: &Namespace) -> Result<()> {
        if self.deployment(n.deployment_id)?.runtime_project_id != n.project_id {
            return Err(conflict("catalog namespace ownership changed"));
        }
        self.db.execute("INSERT INTO catalog_namespaces VALUES (?1,?2,?3) ON CONFLICT(deployment_id,provider_id) DO UPDATE SET record_json=excluded.record_json",params![n.deployment_id.to_string(),n.provider_id,serde_json::to_string(n)?])?;
        Ok(())
    }
    pub(crate) fn recover_catalog_metadata(&mut self) -> Result<()> {
        let records = self
            .db
            .prepare("SELECT record_json FROM catalog_namespaces")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        for record in records {
            let mut n: Namespace = serde_json::from_str(&record)?;
            if n.state.starts_with("creating_") {
                n.state = "indeterminate".into();
                self.save_catalog_namespace(&n)?;
            }
        }
        Ok(())
    }
    pub(crate) fn catalog_asset(&self, owner: DeploymentId, id: OperationId) -> Result<Asset> {
        let row: Option<(String, String)> = self
            .db
            .query_row(
                "SELECT record_json,state FROM catalog_assets WHERE id=?1 AND deployment_id=?2",
                params![id.to_string(), owner.to_string()],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let (record, state) = row.ok_or_else(|| missing("catalog asset in deployment"))?;
        let mut asset: Asset = serde_json::from_str(&record)?;
        asset.state = state;
        Ok(asset)
    }
    pub(crate) fn observe_catalog_assets(
        &mut self,
        context: &Context,
        branch: BranchId,
        mut assets: Vec<Asset>,
        postgres_complete: bool,
    ) -> Result<Vec<Asset>> {
        self.branch_in_project(context.runtime_project_id, branch)?;
        let current = self.branch(branch)?;
        if current.endpoint.desired_state == DesiredState::Deleted
            || current.expired
            || assets
                .iter()
                .any(|a| a.kind == "postgres_table" && a.source_revision != Some(current.revision))
        {
            return Err(conflict(
                "metadata source branch changed during observation",
            ));
        }
        for a in &assets {
            if let Some(epoch) = a.epoch_id {
                if self.snapshot(context.runtime_project_id, epoch)?.state != "available" {
                    return Err(conflict("snapshot retired during metadata observation"));
                }
            }
        }
        if self.deployment(context.deployment_id)?.runtime_project_id != context.runtime_project_id
        {
            return Err(conflict("catalog deployment changed"));
        }
        let tx = self.db.transaction()?;
        if postgres_complete {
            tx.execute("UPDATE catalog_assets SET state='stale' WHERE deployment_id=?1 AND branch_id=?2 AND kind='postgres_table'",params![context.deployment_id.to_string(),branch.to_string()])?;
        }
        for asset in &mut assets {
            if asset.project_id != context.runtime_project_id
                || asset.deployment_id != context.deployment_id
                || asset.branch_id != branch
            {
                return Err(conflict("catalog observation ownership changed"));
            }
            let previous:Option<String>=tx.query_row("SELECT id FROM catalog_assets WHERE deployment_id=?1 AND provider_id=?2 AND resource_key=?3 AND incarnation=?4",params![asset.deployment_id.to_string(),asset.provider_id,asset.resource_key,asset.incarnation],|r|r.get(0)).optional()?;
            if let Some(id) = previous {
                asset.id = parse(&id)?;
            }
            if let Some(epoch) = asset.epoch_id {
                let publication:Option<String>=tx.query_row("SELECT record_json FROM catalog_publications WHERE deployment_id=?1 AND epoch_id=?2 AND state='published'",params![asset.deployment_id.to_string(),epoch.to_string()],|r|r.get(0)).optional()?;
                if let Some(record) = publication {
                    let p: crate::catalog::publication::Publication =
                        serde_json::from_str(&record)?;
                    if let Some(table) = p
                        .tables
                        .iter()
                        .find(|t| t.source_schema == asset.schema && t.source_name == asset.name)
                    {
                        asset.uc_object_id = Some(table.id.to_string());
                        use sha2::{Digest, Sha256};
                        asset.version =
                            hex::encode(Sha256::digest(serde_json::to_vec(&serde_json::json!([
                                asset.version,
                                asset.uc_object_id
                            ]))?));
                    }
                }
            }
            asset.state = "active".into();
            tx.execute("INSERT INTO catalog_assets VALUES (?1,?2,?3,?4,?5,?6,?7,?8,'active',?9) ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json,state='active'",params![asset.id.to_string(),asset.deployment_id.to_string(),asset.project_id.to_string(),asset.branch_id.to_string(),asset.provider_id,asset.resource_key,asset.incarnation,asset.kind,serde_json::to_string(asset)?])?;
        }
        tx.commit()?;
        Ok(assets)
    }
    pub(crate) fn stale_catalog_asset(
        &mut self,
        owner: DeploymentId,
        id: OperationId,
    ) -> Result<()> {
        self.db.execute(
            "UPDATE catalog_assets SET state='stale' WHERE deployment_id=?1 AND id=?2",
            params![owner.to_string(), id.to_string()],
        )?;
        Ok(())
    }
}
