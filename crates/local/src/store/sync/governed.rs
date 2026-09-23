//! Branch-scoped service authority. Browser authentication authorizes admission only.
use super::super::{authorization as a, governed::grant};
use super::*;
use crate::{
    governed::Capability,
    identity::Context,
    sync::{GovernedCommand, ServiceAuthority},
};

impl Store {
    pub(crate) fn sync_authority_live(&self, p: &Policy) -> Result<()> {
        let Some(authority) = &p.service_authority else {
            return if self.governed_branch(p.branch_id)? {
                Err(crate::governed::denied())
            } else {
                Ok(())
            };
        };
        service_live(&self.db, p, authority)
    }
    pub(crate) fn governed_sync(
        &mut self,
        ctx: &Context,
        deployment: &str,
        request: GovernedCommand,
    ) -> Result<Value> {
        a::require(&self.db, ctx, deployment, 1)?;
        if request.service_principal.is_some() && !matches!(request.command, Command::Create { .. })
        {
            return Err(crate::governed::denied());
        }
        let d = self.deployment(deployment.parse().map_err(|_| crate::governed::denied())?)?;
        let project = d.runtime_project_id;
        let now = crate::identity::now();
        if matches!(request.command, Command::List) {
            let mut policies = Vec::new();
            for p in self
                .sync_policies()?
                .into_iter()
                .filter(|p| p.deployment_id == d.deployment_id && p.service_authority.is_some())
            {
                if grant(
                    &self.db,
                    ctx,
                    deployment,
                    &p.branch_id.to_string(),
                    Capability::ReadSync,
                )
                .is_ok()
                {
                    policies.push(self.sync_policy_view(&p, now)?);
                }
            }
            return Ok(json!(policies));
        }
        let policy_id = match &request.command {
            Command::Create { .. } | Command::Inspect { .. } | Command::List => None,
            Command::Cancel { id, .. } | Command::Run { id } => {
                Some(self.sync_run(project, *id)?.policy_id)
            }
            Command::Update { id, .. }
            | Command::Pause { id, .. }
            | Command::Resume { id, .. }
            | Command::Delete { id, .. }
            | Command::RunNow { id, .. }
            | Command::Get { id }
            | Command::Runs { id, .. }
            | Command::ReviewResync { id }
            | Command::Resync { id, .. } => Some(*id),
        };
        if let Some(id) = policy_id {
            let p = self.sync_policy(project, id)?;
            if p.deployment_id != d.deployment_id || p.service_authority.is_none() {
                return Err(crate::governed::denied());
            }
        }
        let b = match &request.command {
            Command::Create { branch, .. } | Command::Inspect { branch } => {
                let id = branch.parse().map_err(|_| crate::governed::denied())?;
                self.branch_in_project(project, id)?.branch.id
            }
            Command::Cancel { id, .. } | Command::Run { id } => {
                self.sync_run(project, *id)?.branch_id
            }
            Command::Update { id, .. }
            | Command::Pause { id, .. }
            | Command::Resume { id, .. }
            | Command::Delete { id, .. }
            | Command::RunNow { id, .. }
            | Command::Get { id }
            | Command::Runs { id, .. }
            | Command::ReviewResync { id }
            | Command::Resync { id, .. } => self.sync_policy(project, *id)?.branch_id,
            Command::List => unreachable!(),
        };
        // No project membership or management role implies source or result access.
        let cap = if request.command.key().is_some() {
            Capability::ManageSync
        } else {
            Capability::ReadSync
        };
        grant(&self.db, ctx, deployment, &b.to_string(), cap)?;
        if request.command.key().is_some() {
            grant(&self.db, ctx, deployment, &b.to_string(), Capability::Read)?;
        }
        if let Some(id) = policy_id {
            if request.command.key().is_some()
                && !matches!(
                    request.command,
                    Command::Pause { .. } | Command::Delete { .. } | Command::Cancel { .. }
                )
            {
                self.sync_source(&self.sync_policy(project, id)?)?;
            }
        }
        let incremental = match &request.command {
            Command::Create { config, .. } | Command::Update { config, .. } => config.incremental(),
            Command::Resume { id, .. } => self.sync_policy(project, *id)?.config.incremental(),
            _ => false,
        };
        if incremental
            && (!crate::sync::governed_incremental_available(self)
                || self.branch(b)?.ports.is_none())
        {
            return Err(conflict(
                "governed incremental sync requires a matching native analytical runtime",
            ));
        }
        if let Command::Inspect { branch } = &request.command {
            let mut v = self.inspect_sync(project, d.deployment_id, branch)?;
            v["capabilities"] = json!({"sync_controls":1,"managed_snapshot_scheduling":true,"incremental_triggered":crate::sync::governed_incremental_available(self),"continuous_sync":crate::sync::governed_incremental_available(self),"sync_event_triggers":false});
            return Ok(v);
        }
        let authority = if let Command::Create { .. } = &request.command {
            let principal = request
                .service_principal
                .as_ref()
                .ok_or_else(crate::governed::denied)?;
            let generation = self.db.query_row("SELECT sync_generation FROM identity_principals WHERE id=?1 AND kind='service' AND disabled=0", [principal], |r|r.get(0)).optional()?.ok_or_else(crate::governed::denied)?;
            let authority = ServiceAuthority {
                principal_id: principal.clone(),
                realm_id: ctx.realm_id.clone(),
                generation,
                policy_revision: a::policy(&self.db, deployment)?,
            };
            let service = service_context(&self.db, &authority)?;
            for cap in [Capability::Read, Capability::ExecuteSync] {
                grant(&self.db, &service, deployment, &b.to_string(), cap)?;
            }
            Some(authority)
        } else {
            None
        };
        let key = request.command.key().map(str::to_owned);
        if let Some(key) = &key {
            if let Some(value) = a::replay(&self.db, ctx, deployment, key, &request)? {
                if matches!(request.command, Command::Create { .. }) {
                    let p: Policy = serde_json::from_value(value.clone())?;
                    self.sync_authority_live(&p)?;
                }
                return Ok(value);
            }
            a::expected(
                &self.db,
                deployment,
                request
                    .expected_policy
                    .ok_or_else(crate::governed::denied)?,
            )?;
        }
        // Both journals commit together. Scope internal keys by actor and deployment.
        let mut command = request.command.clone();
        let encoded = crate::project_apply::digest(&json!([ctx.actor_id, deployment, key]))?;
        match &mut command {
            Command::Create { key, .. }
            | Command::Update { key, .. }
            | Command::Pause { key, .. }
            | Command::Resume { key, .. }
            | Command::Delete { key, .. }
            | Command::RunNow { key, .. }
            | Command::Cancel { key, .. }
            | Command::Resync { key, .. } => *key = format!("governed-sync:{encoded}"),
            _ => (),
        }
        self.db.execute_batch("SAVEPOINT governed_sync")?;
        let result = (|| {
            let value =
                self.sync_command_authority(project, d.deployment_id, command, now, authority)?;
            if let Some(key) = &key {
                a::audit(
                    &self.db,
                    ctx,
                    deployment,
                    a::policy(&self.db, deployment)?,
                    &format!(
                        "sync.{}",
                        serde_json::to_value(&request.command)?["kind"]
                            .as_str()
                            .unwrap()
                    ),
                    key,
                    &b.to_string(),
                    request
                        .service_principal
                        .as_deref()
                        .unwrap_or(&ctx.actor_id),
                )?;
                a::receipt(&self.db, ctx, deployment, key, &request, &value)?;
            }
            Ok(value)
        })();
        match result {
            Ok(v) => {
                self.db.execute_batch("RELEASE governed_sync")?;
                Ok(v)
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO governed_sync; RELEASE governed_sync")?;
                Err(e)
            }
        }
    }
    /// Recover the durable run mapping even before its export link is committed.
    pub(crate) fn sync_export_policy(&self, id: OperationId) -> Result<Option<Policy>> {
        policy_for_artifact(&self.db, id)
    }
    pub(crate) fn governed_sync_export(
        &self,
        ctx: &Context,
        deployment: &str,
        id: OperationId,
    ) -> Result<bool> {
        let Some(p) = self.sync_export_policy(id)? else {
            return Ok(false);
        };
        if p.deployment_id.to_string() != deployment || p.service_authority.is_none() {
            return Err(crate::governed::denied());
        }
        grant(
            &self.db,
            ctx,
            deployment,
            &p.branch_id.to_string(),
            Capability::ReadSync,
        )?;
        self.sync_authority_live(&p)?;
        Ok(true)
    }
}

pub(super) fn service_context(
    db: &rusqlite::Connection,
    authority: &ServiceAuthority,
) -> Result<Context> {
    if !db.prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND kind='service' AND disabled=0 AND sync_generation=?2")?.exists(params![authority.principal_id,authority.generation])? {
            return Err(crate::governed::denied());
        }
    let ctx = Context {
        api_version: crate::identity::VERSION,
        realm_id: authority.realm_id.clone(),
        actor_id: authority.principal_id.clone(),
        effective_principal_id: authority.principal_id.clone(),
        channel: crate::identity::Channel::Service,
        scopes: vec![crate::authorization::CONTROL_SCOPE.into()],
        expires_ms: i64::MAX,
    };
    a::validate_context(db, &ctx)?;
    Ok(ctx)
}

pub(crate) fn service_live(
    db: &rusqlite::Connection,
    p: &Policy,
    authority: &ServiceAuthority,
) -> Result<()> {
    let ctx = service_context(db, authority)?;
    a::expected(db, &p.deployment_id.to_string(), authority.policy_revision)?;
    for cap in [Capability::Read, Capability::ExecuteSync] {
        grant(
            db,
            &ctx,
            &p.deployment_id.to_string(),
            &p.branch_id.to_string(),
            cap,
        )?;
    }
    Ok(())
}

pub(crate) fn policy_for_artifact(
    db: &rusqlite::Connection,
    id: OperationId,
) -> Result<Option<Policy>> {
    let record: Option<String> = db.query_row("SELECT p.record FROM sync_policies p JOIN sync_runs r ON r.policy_id=p.id JOIN operations o ON o.id=?1 WHERE r.refresh_id=o.id OR o.request_key='internal:sync:' || r.id UNION ALL SELECT p.record FROM sync_policies p JOIN sync_captures c ON c.policy_id=p.id JOIN incremental_runs r ON r.capture_id=c.id WHERE r.id=?1 UNION ALL SELECT p.record FROM sync_policies p JOIN sync_captures c ON c.policy_id=p.id JOIN operations o ON o.id=?1 WHERE c.bootstrap_id=o.id OR o.request_key='internal:capture-bootstrap:' || c.id LIMIT 1", [id.to_string()], |r|r.get(0)).optional()?;
    record.map(|v| Ok(serde_json::from_str(&v)?)).transpose()
}
