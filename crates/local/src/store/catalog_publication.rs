use super::*;
use crate::catalog::publication::Publication as CatalogPublication;
impl Store {
    pub(crate) fn catalog_usage(&self) -> Result<serde_json::Value> {
        crate::catalog::recovery::usage(self.root(), &self.db)
    }
    pub(crate) fn catalog_dataset_choices(
        &self,
        owner: Option<DeploymentId>,
        after: Option<&str>,
    ) -> Result<serde_json::Value> {
        if after.is_some_and(|a| a.parse::<OperationId>().is_err()) {
            return Err(invalid("invalid dataset discovery cursor"));
        }
        let mut query = self.db.prepare("SELECT record_json FROM catalog_publications WHERE (?1 IS NULL OR deployment_id=?1) AND id>?2 AND state!='retired' ORDER BY id LIMIT 51")?;
        let rows = query
            .query_map(
                params![owner.map(|o| o.to_string()), after.unwrap_or("")],
                |r| r.get::<_, String>(0),
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let more = rows.len() > 50;
        let mut items = vec![];
        for raw in rows.iter().take(50) {
            let p: CatalogPublication = serde_json::from_str(raw)?;
            let (head_revision, head) = self.catalog_head(p.deployment_id, p.branch_id)?;
            let name = self.project(p.project_id)?.name;
            items.push(serde_json::json!({"target":{"deployment_id":p.deployment_id,"provider_id":p.namespace.provider_id,"publication_id":p.id},"owner":name,"branch_id":p.branch_id,"branch":self.branch(p.branch_id)?.branch.name,"epoch_id":p.epoch_id,"revision":p.revision,"snapshot_at_ms":p.snapshot_at_ms,"state":p.state,"needs_attention":p.error.is_some(),"head":head==Some(p.id),"binding_revision":head_revision,"table_count":p.tables.len()}));
        }
        let next = if more {
            items.last().map(|v| v["target"]["publication_id"].clone())
        } else {
            None
        };
        Ok(serde_json::json!({"api_version":1,"items":items,"next":next}))
    }

    pub(crate) fn dataset_holders(&self, id: OperationId) -> Result<Vec<serde_json::Value>> {
        let keys=self.db.prepare("SELECT reference_key FROM catalog_publication_refs WHERE publication_id=?1 ORDER BY reference_key LIMIT 529")?
            .query_map([id.to_string()],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(keys.into_iter().map(|key|{let parts:Vec<_>=key.splitn(3,':').collect();
            serde_json::json!({"kind":parts.first(),"owner_id":parts.get(1),"logical":parts.get(2)})}).collect())
    }
    pub(crate) fn dataset_other_references(&self, epoch: EpochId) -> Result<serde_json::Value> {
        let active:i64=self.db.query_row("SELECT count(*) FROM analytical_sessions WHERE epoch_id=?1 AND state IN ('waiting','starting','ready','closing')",[epoch.to_string()],|r|r.get(0))?;
        let snapshots: i64 = self.db.query_row(
            "SELECT count(*) FROM snapshot_leases WHERE epoch_id=?1 AND expires_at_ms>?2",
            params![epoch.to_string(), now_ms()?],
            |r| r.get(0),
        )?;
        let leases: i64 = self.db.query_row(
            "SELECT count(*) FROM leases WHERE epoch_id=?1 AND expires_at_ms>?2",
            params![epoch.to_string(), now_ms()?],
            |r| r.get(0),
        )?;
        Ok(
            serde_json::json!({"primary_sessions":active,"snapshot_leases":snapshots,"epoch_leases":leases}),
        )
    }
    pub(crate) fn dataset_references(&self, id: OperationId) -> Result<serde_json::Value> {
        let mut counts = std::collections::BTreeMap::<String, i64>::new();
        for kind in ["binding", "apply", "session"] {
            let count=self.db.query_row("SELECT count(*) FROM catalog_publication_refs WHERE publication_id=?1 AND reference_key LIKE ?2",params![id.to_string(),format!("{kind}:%")],|r|r.get(0))?;
            counts.insert(kind.into(), count);
        }
        Ok(serde_json::json!(counts))
    }

    pub(crate) fn catalog_head(
        &self,
        owner: DeploymentId,
        branch: BranchId,
    ) -> Result<(i64, Option<OperationId>)> {
        let r:Option<(i64,Option<String>)>=self.db.query_row("SELECT revision,publication_id FROM catalog_heads WHERE deployment_id=?1 AND branch_id=?2",params![owner.to_string(),branch.to_string()],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        let (revision, id) = r.unwrap_or((0, None));
        Ok((revision, id.map(|id| parse(&id)).transpose()?))
    }
    pub(crate) fn catalog_publication(
        &self,
        owner: DeploymentId,
        id: OperationId,
    ) -> Result<CatalogPublication> {
        let s: String = self
            .db
            .query_row(
                "SELECT record_json FROM catalog_publications WHERE deployment_id=?1 AND id=?2",
                params![owner.to_string(), id.to_string()],
                |r| r.get(0),
            )
            .optional()?
            .ok_or_else(|| missing("catalog publication in deployment"))?;
        Ok(serde_json::from_str(&s)?)
    }
    pub(crate) fn catalog_publication_epoch(
        &self,
        owner: DeploymentId,
        epoch: EpochId,
    ) -> Result<CatalogPublication> {
        let text: String = self.db.query_row(
            "SELECT record_json FROM catalog_publications WHERE deployment_id=?1 AND epoch_id=?2",
            params![owner.to_string(),epoch.to_string()],|r|r.get(0)).optional()?
            .ok_or_else(|| missing("catalog publication for epoch in deployment"))?;
        Ok(serde_json::from_str(&text)?)
    }
    pub(crate) fn catalog_publication_key(
        &self,
        owner: DeploymentId,
        key: &str,
    ) -> Result<Option<CatalogPublication>> {
        let s:Option<String>=self.db.query_row("SELECT record_json FROM catalog_publications WHERE deployment_id=?1 AND request_key=?2",params![owner.to_string(),key],|r|r.get(0)).optional()?;
        s.map(|s| serde_json::from_str(&s).map_err(Into::into))
            .transpose()
    }
    pub(crate) fn begin_catalog_publication(&mut self, p: &CatalogPublication) -> Result<()> {
        let s = self.snapshot(p.project_id, p.epoch_id)?;
        if s.state != "available"
            || s.publication.branch_id != p.branch_id
            || self.deployment(p.deployment_id)?.runtime_project_id != p.project_id
        {
            return Err(conflict("publication ownership or snapshot changed"));
        }
        let count: i64 = self.db.query_row(
            "SELECT count(*) FROM catalog_publications WHERE state!='retired'",
            [],
            |r| r.get(0),
        )?;
        if count >= 128 {
            return Err(conflict(
                "catalog publication retention limit reached; retire unused revisions first",
            ));
        }
        if self
            .db
            .prepare("SELECT 1 FROM catalog_publications WHERE deployment_id=?1 AND epoch_id=?2")?
            .exists(params![p.deployment_id.to_string(), p.epoch_id.to_string()])?
        {
            return Err(conflict(
                "this epoch already has a catalog publication; use its request key or publish a new snapshot",
            ));
        }
        let usage = self.catalog_usage()?;
        let descriptor = s
            .publication
            .descriptor
            .as_ref()
            .ok_or_else(|| invalid("snapshot descriptor missing"))?;
        let bytes = descriptor["manifest"]["files"]
            .as_array()
            .ok_or_else(|| invalid("snapshot inventory missing"))?
            .iter()
            .try_fold(0u64, |sum, f| {
                sum.checked_add(f["bytes"].as_u64().unwrap_or(u64::MAX))
            })
            .ok_or_else(|| invalid("snapshot size exceeds catalog budget"))?;
        use crate::catalog::recovery::{MAX_METADATA_BYTES, MAX_PUBLICATIONS, MAX_RETAINED_BYTES};
        if usage["metadata_bytes"].as_u64().unwrap_or(u64::MAX) >= MAX_METADATA_BYTES
            || usage["publication_records"].as_u64().unwrap_or(u64::MAX) >= MAX_PUBLICATIONS
            || usage["retained_snapshot_bytes"]
                .as_u64()
                .unwrap_or(u64::MAX)
                .saturating_add(bytes)
                > MAX_RETAINED_BYTES
        {
            return Err(conflict(
                "catalog capacity reached; retire unused publications or export to a fresh installation; existing data is retained",
            ));
        }
        let tx = self.db.transaction()?;
        // Admission and GC are serialized by this writer. Pins have no clock expiry.
        tx.execute(
            "INSERT INTO catalog_publications VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![
                p.id.to_string(),
                p.deployment_id.to_string(),
                p.branch_id.to_string(),
                p.epoch_id.to_string(),
                p.key,
                p.state,
                serde_json::to_string(p)?
            ],
        )?;
        tx.execute(
            "INSERT INTO catalog_retention VALUES (?1,?2)",
            params![p.id.to_string(), p.epoch_id.to_string()],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub(crate) fn save_catalog_publication(&mut self, p: &CatalogPublication) -> Result<()> {
        self.db.execute("UPDATE catalog_publications SET state=?3,record_json=?4 WHERE deployment_id=?1 AND id=?2",params![p.deployment_id.to_string(),p.id.to_string(),p.state,serde_json::to_string(p)?])?;
        Ok(())
    }
    pub(crate) fn pending_catalog_publications(&self) -> Result<Vec<CatalogPublication>> {
        let rows=self.db.prepare("SELECT record_json FROM catalog_publications WHERE state IN ('registering','retiring') ORDER BY rowid LIMIT 256")?.query_map([],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
        rows.into_iter()
            .map(|s| serde_json::from_str(&s).map_err(Into::into))
            .collect()
    }
    pub(crate) fn commit_catalog_publication(&mut self, p: &mut CatalogPublication) -> Result<()> {
        self.data_export_live(
            self.snapshot(p.project_id, p.epoch_id)?
                .publication
                .export_id,
            true,
        )?;
        let b = self.branch_in_project(p.project_id, p.branch_id)?;
        let (revision, _) = self.catalog_head(p.deployment_id, p.branch_id)?;
        if b.revision != p.source_revision
            || b.expired
            || b.endpoint.desired_state == DesiredState::Deleted
            || revision != p.expected_binding_revision
            || self.snapshot(p.project_id, p.epoch_id)?.state != "available"
            || p.tables.iter().any(|t| t.state != "verified")
        {
            return Err(conflict(
                "source, binding or snapshot changed before catalog commit",
            ));
        }
        p.state = "published".into();
        p.revision = Some(revision + 1);
        let tx = self.db.transaction()?;
        tx.execute("INSERT INTO catalog_heads VALUES (?1,?2,?3,?4) ON CONFLICT(deployment_id,branch_id) DO UPDATE SET revision=excluded.revision,publication_id=excluded.publication_id",params![p.deployment_id.to_string(),p.branch_id.to_string(),revision+1,p.id.to_string()])?;
        tx.execute(
            "UPDATE catalog_publications SET state='published',record_json=?2 WHERE id=?1",
            params![p.id.to_string(), serde_json::to_string(p)?],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub(crate) fn retire_catalog_publication(
        &mut self,
        owner: DeploymentId,
        id: OperationId,
        key: &str,
        expected: i64,
    ) -> Result<CatalogPublication> {
        let mut p = self.catalog_publication(owner, id)?;
        if let Some(old) = &p.unpublish_key {
            if old != key || p.unpublish_binding_revision != Some(expected) {
                return Err(conflict("unpublish already requested with another key"));
            }
            return Ok(p);
        }
        let (revision, head) = self.catalog_head(owner, p.branch_id)?;
        if revision != expected {
            return Err(conflict("catalog binding revision changed"));
        }
        p.state = "retiring".into();
        p.error = None;
        p.unpublish_key = Some(key.into());
        p.unpublish_binding_revision = Some(expected);
        let tx = self.db.transaction()?;
        if head == Some(id) {
            tx.execute("UPDATE catalog_heads SET revision=revision+1,publication_id=NULL WHERE deployment_id=?1 AND branch_id=?2",params![owner.to_string(),p.branch_id.to_string()])?;
        }
        tx.execute(
            "UPDATE catalog_publications SET state='retiring',record_json=?2 WHERE id=?1",
            params![id.to_string(), serde_json::to_string(&p)?],
        )?;
        tx.commit()?;
        Ok(p)
    }
    pub(crate) fn catalog_references(&self, p: &CatalogPublication) -> Result<bool> {
        Ok(self.db.query_row("SELECT EXISTS(SELECT 1 FROM catalog_publication_refs WHERE publication_id=?1) OR EXISTS(SELECT 1 FROM snapshot_leases WHERE epoch_id=?2 AND expires_at_ms>?3) OR EXISTS(SELECT 1 FROM leases WHERE epoch_id=?2 AND expires_at_ms>?3) OR EXISTS(SELECT 1 FROM analytical_sessions WHERE epoch_id=?2 AND state IN ('waiting','starting','ready','closing'))",params![p.id.to_string(),p.epoch_id.to_string(),now_ms()?],|r|r.get(0))?)
    }
    /// Used by later catalog consumers; admission and unpublish share the writer.
    #[allow(dead_code)]
    pub(crate) fn pin_catalog_publication(
        &mut self,
        owner: DeploymentId,
        id: OperationId,
        key: &str,
    ) -> Result<()> {
        let p = self.catalog_publication(owner, id)?;
        if p.state != "published" {
            return Err(conflict(
                "catalog publication is not available for new references",
            ));
        }
        self.db.execute(
            "INSERT INTO catalog_publication_refs VALUES (?1,?2) ON CONFLICT DO NOTHING",
            params![id.to_string(), key],
        )?;
        Ok(())
    }
    #[allow(dead_code)]
    pub(crate) fn release_catalog_publication(
        &mut self,
        owner: DeploymentId,
        id: OperationId,
        key: &str,
    ) -> Result<()> {
        self.catalog_publication(owner, id)?;
        self.db.execute(
            "DELETE FROM catalog_publication_refs WHERE publication_id=?1 AND reference_key=?2",
            params![id.to_string(), key],
        )?;
        Ok(())
    }
    pub(crate) fn finish_catalog_retirement(&mut self, p: &mut CatalogPublication) -> Result<()> {
        if self.catalog_references(p)? || p.tables.iter().any(|t| t.state != "deleted") {
            return Err(conflict(
                "catalog retirement still referenced or incomplete",
            ));
        }
        p.state = "retired".into();
        p.error = None;
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE catalog_publications SET state='retired',record_json=?2 WHERE id=?1",
            params![p.id.to_string(), serde_json::to_string(p)?],
        )?;
        tx.execute(
            "DELETE FROM catalog_retention WHERE publication_id=?1",
            [p.id.to_string()],
        )?;
        tx.commit()?;
        Ok(())
    }
}
