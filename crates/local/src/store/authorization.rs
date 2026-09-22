use super::*;
use crate::{
    authorization::{self as auth, AdminCommand, Command, Grant, Role, Subject},
    identity::{self, Context},
};
use serde_json::{Value, json};

pub(super) fn policy(db: &Connection, deployment: &str) -> Result<i64> {
    db.query_row(
        "SELECT revision FROM authorization_policy WHERE deployment=?1",
        [deployment],
        |r| r.get(0),
    )
    .optional()?
    .ok_or_else(auth::denied)
}
fn owner(db: &Connection) -> Result<Context> {
    let (realm, id): (String, String) =
        db.query_row("SELECT id,local_owner FROM identity_realm", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })?;
    Ok(Context {
        api_version: identity::VERSION,
        realm_id: realm,
        actor_id: id.clone(),
        effective_principal_id: id,
        channel: identity::Channel::Cli,
        scopes: vec![auth::CONTROL_SCOPE.into()],
        expires_ms: i64::MAX,
    })
}
pub(super) fn validate_context(db: &Connection, ctx: &Context) -> Result<()> {
    let realm: String = db.query_row("SELECT id FROM identity_realm", [], |r| r.get(0))?;
    let local: String = db.query_row("SELECT local_owner FROM identity_realm", [], |r| r.get(0))?;
    if ctx.actor_id != local {
        super::security::admission(db)?;
    }
    if ctx.realm_id != realm
        || ctx.actor_id != ctx.effective_principal_id
        || ctx.expires_ms <= identity::now()
        || !ctx.scopes.iter().any(|s| s == auth::CONTROL_SCOPE)
        || !db
            .prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND disabled=0")?
            .exists([&ctx.actor_id])?
    {
        return Err(auth::denied());
    }
    Ok(())
}
fn is_owner(db: &Connection, ctx: &Context) -> Result<bool> {
    Ok(ctx.actor_id == owner(db)?.actor_id)
}
pub(super) fn subject(db: &Connection, subject: &Subject) -> Result<String> {
    let exists = match subject {
        Subject::Principal(id) => db
            .prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND kind!='local_owner'")?
            .exists([id])?,
        Subject::Group(id) => db
            .prepare("SELECT 1 FROM identity_groups WHERE id=?1")?
            .exists([id])?,
    };
    if !exists {
        return Err(auth::denied());
    }
    Ok(subject.key())
}
pub(super) fn subjects(db: &Connection, id: &str) -> Result<Vec<String>> {
    let mut values = vec![format!("principal:{id}")];
    for group in db
        .prepare("SELECT group_id FROM identity_memberships WHERE principal=?1")?
        .query_map([id], |r| r.get::<_, String>(0))?
    {
        values.push(format!("group:{}", group?));
    }
    Ok(values)
}
pub(super) fn role(db: &Connection, id: &str, deployment: &str) -> Result<u8> {
    let mut rank = 0;
    for subject in subjects(db, id)? {
        let value: Option<String> = db
            .query_row(
                "SELECT role FROM authorization_roles WHERE deployment=?1 AND subject=?2",
                params![deployment, subject],
                |r| r.get(0),
            )
            .optional()?;
        rank = rank.max(match value.as_deref() {
            Some("viewer") => 1,
            Some("editor") => 2,
            Some("administrator") => 3,
            _ => 0,
        });
    }
    Ok(rank)
}
pub(super) fn require(db: &Connection, ctx: &Context, deployment: &str, rank: u8) -> Result<()> {
    validate_context(db, ctx)?;
    policy(db, deployment)?;
    if !is_owner(db, ctx)? && role(db, &ctx.actor_id, deployment)? < rank {
        return Err(auth::denied());
    }
    Ok(())
}
pub(super) fn granted(
    db: &Connection,
    id: &str,
    deployment: &str,
    grant: Grant,
    effective: &str,
    source: &str,
) -> Result<bool> {
    for subject in subjects(db, id)? {
        if db.prepare("SELECT 1 FROM authorization_grants WHERE deployment=?1 AND subject=?2 AND capability=?3 AND effective_principal=?4 AND source_revision=?5")?.exists(params![deployment,subject,grant.name(),effective,source])? {return Ok(true);}
    }
    Ok(false)
}
pub(super) fn audit(
    db: &Connection,
    ctx: &Context,
    deployment: &str,
    revision: i64,
    action: &str,
    key: &str,
    target: &str,
    effective: &str,
) -> Result<()> {
    db.execute("INSERT INTO authorization_audit(at_ms,realm,deployment,actor,effective_principal,policy_revision,action,request_key,target) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",params![identity::now(),ctx.realm_id,deployment,ctx.actor_id,effective,revision,action,key,target])?;
    Ok(())
}
pub(super) fn replay(
    db: &Connection,
    ctx: &Context,
    deployment: &str,
    key: &str,
    request: &impl Serialize,
) -> Result<Option<Value>> {
    auth::key(key)?;
    let saved:Option<(String,String)>=db.query_row("SELECT request_hash,result FROM authorization_mutations WHERE deployment=?1 AND actor=?2 AND request_key=?3",params![deployment,ctx.actor_id,key],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
    if let Some((digest, result)) = saved {
        if digest != crate::project_apply::digest(request)? {
            return Err(conflict(
                "request key already belongs to a different authorized mutation",
            ));
        }
        return Ok(Some(serde_json::from_str(&result)?));
    }
    Ok(None)
}
pub(super) fn receipt(
    db: &Connection,
    ctx: &Context,
    deployment: &str,
    key: &str,
    request: &impl Serialize,
    result: &Value,
) -> Result<()> {
    db.execute(
        "INSERT INTO authorization_mutations VALUES (?1,?2,?3,?4,?5)",
        params![
            deployment,
            ctx.actor_id,
            key,
            crate::project_apply::digest(request)?,
            serde_json::to_string(result)?
        ],
    )?;
    Ok(())
}
pub(super) fn expected(db: &Connection, deployment: &str, revision: i64) -> Result<()> {
    if policy(db, deployment)? != revision {
        return Err(conflict(
            "project policy changed; refresh before retrying with a new request key",
        ));
    }
    Ok(())
}
fn set_role(
    db: &Connection,
    deployment: &str,
    subject: &Subject,
    value: &Option<Role>,
) -> Result<()> {
    let subject = super::authorization::subject(db, subject)?;
    if let Some(role) = value {
        db.execute("INSERT INTO authorization_roles VALUES (?1,?2,?3) ON CONFLICT(deployment,subject) DO UPDATE SET role=excluded.role",params![deployment,subject,role.name()])?;
    } else {
        db.execute(
            "DELETE FROM authorization_roles WHERE deployment=?1 AND subject=?2",
            params![deployment, subject],
        )?;
    }
    db.execute(
        "UPDATE authorization_policy SET revision=revision+1 WHERE deployment=?1",
        [deployment],
    )?;
    Ok(())
}
fn project(db: &Connection, deployment: &str) -> Result<Value> {
    let (name,target):(String,String)=db.query_row("SELECT p.name,d.target FROM deployments d JOIN project_definitions p ON p.id=d.definition_id WHERE d.id=?1",[deployment],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or_else(auth::denied)?;
    Ok(
        json!({"deployment_id":deployment,"name":name,"target":target,"policy_revision":policy(db,deployment)?,"workload_launch_enabled":false,"isolated_execution":"operator_opt_in"}),
    )
}
fn read_policy(db: &Connection, deployment: &str) -> Result<Value> {
    let revision = policy(db, deployment)?;
    let roles = db
        .prepare(
            "SELECT subject,role FROM authorization_roles WHERE deployment=?1 ORDER BY subject",
        )?
        .query_map([deployment], |r| {
            Ok(json!({"subject":r.get::<_,String>(0)?,"role":r.get::<_,String>(1)?}))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let grants=db.prepare("SELECT subject,capability,effective_principal,source_revision FROM authorization_grants WHERE deployment=?1 ORDER BY subject,capability,effective_principal,source_revision")?.query_map([deployment],|r|Ok(json!({"subject":r.get::<_,String>(0)?,"capability":r.get::<_,String>(1)?,"effective_principal":r.get::<_,String>(2)?,"source_revision":r.get::<_,String>(3)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
    let data_grants=db.prepare("SELECT branch,subject,capability FROM data_grants WHERE deployment=?1 ORDER BY branch,subject,capability")?.query_map([deployment],|r|Ok(json!({"branch":r.get::<_,String>(0)?,"subject":r.get::<_,String>(1)?,"capability":r.get::<_,String>(2)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(
        json!({"deployment_id":deployment,"policy_revision":revision,"roles":roles,"grants":grants,"data_grants":data_grants}),
    )
}
fn execution(db: &Connection, deployment: &str, id: &str) -> Result<Value> {
    db.query_row("SELECT a.actor,a.effective_principal,a.source_revision,a.policy_revision,a.state,e.state FROM authorization_executions a LEFT JOIN isolated_executions e ON e.id=a.id WHERE a.deployment=?1 AND a.id=?2",params![deployment,id],|r|{
        let runtime:Option<String>=r.get(5)?;
        Ok(json!({"id":id,"deployment_id":deployment,"actor_id":r.get::<_,String>(0)?,"effective_principal_id":r.get::<_,String>(1)?,"source_revision":r.get::<_,String>(2)?,"policy_revision":r.get::<_,i64>(3)?,"state":r.get::<_,String>(4)?,"runtime_started":runtime.as_deref().is_some_and(|s|matches!(s,"running"|"finished")),"runtime_state":runtime}))
    }).optional()?.ok_or_else(auth::denied)
}
fn execution_access(db: &Connection, ctx: &Context, deployment: &str, value: &Value) -> Result<()> {
    if value["actor_id"].as_str() != Some(&ctx.actor_id)
        && !is_owner(db, ctx)?
        && !granted(db, &ctx.actor_id, deployment, Grant::StopAny, "", "")?
    {
        return Err(auth::denied());
    }
    Ok(())
}
impl Store {
    /// Prepare authentication outside the sole writer, then authorize exactly the
    /// requested operation with a fresh context on the writer before any effect.
    pub(crate) fn authorization_job(
        &mut self,
        envelope: auth::Envelope,
    ) -> Result<super::identity::IdentityJob> {
        if envelope.api_version != auth::VERSION {
            return Err(invalid("unsupported project authorization API version"));
        }
        let authenticate = self.identity_job(identity::AuthCommand::Authenticate {
            token: envelope.token,
            channel: envelope.channel,
            csrf: envelope.csrf,
        })?;
        Ok(Box::new(move || {
            let finish = authenticate()?;
            Ok(Box::new(move |store: &mut Store| {
                let context: Context = serde_json::from_value(finish(store)?)?;
                store.authorized_command(&context, envelope.command)
            }))
        }))
    }
    /// The OS-owned control socket is the only origin of the legacy owner context.
    pub(crate) fn authorize_operator_route(&self, request: &crate::daemon::Request) -> Result<()> {
        if crate::authorization::inventory::boundary(request) == auth::inventory::Boundary::Operator
        {
            validate_context(&self.db, &owner(&self.db)?)?;
        }
        Ok(())
    }
    pub fn authorization_admin(&mut self, command: AdminCommand) -> Result<Value> {
        let ctx = owner(&self.db)?;
        validate_context(&self.db, &ctx)?;
        let tx = self.db.transaction()?;
        let result = match &command {
            AdminCommand::SetDataGrant {
                deployment,
                branch,
                subject: who,
                capability,
                present,
                expected_policy,
                key,
            } => super::governed::set_grant(
                &tx,
                &ctx,
                &command,
                deployment,
                branch,
                who,
                *capability,
                *present,
                *expected_policy,
                key,
            )?,
            AdminCommand::Policy { deployment } => read_policy(&tx, deployment)?,
            AdminCommand::Audit { deployment, after } => {
                policy(&tx, deployment)?;
                let events=tx.prepare("SELECT sequence,actor,effective_principal,policy_revision,action,request_key,target FROM authorization_audit WHERE deployment=?1 AND sequence>?2 ORDER BY sequence LIMIT 200")?.query_map(params![deployment,after],|r|Ok(json!({"sequence":r.get::<_,i64>(0)?,"actor_id":r.get::<_,String>(1)?,"effective_principal_id":r.get::<_,String>(2)?,"policy_revision":r.get::<_,i64>(3)?,"action":r.get::<_,String>(4)?,"request_key":r.get::<_,String>(5)?,"target":r.get::<_,String>(6)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
                json!({"events":events})
            }
            AdminCommand::SetRole {
                deployment,
                subject,
                role,
                expected_policy,
                key,
            } => {
                if let Some(result) = replay(&tx, &ctx, deployment, key, &command)? {
                    return Ok(result);
                }
                expected(&tx, deployment, *expected_policy)?;
                set_role(&tx, deployment, subject, role)?;
                audit(
                    &tx,
                    &ctx,
                    deployment,
                    *expected_policy,
                    "policy.role",
                    key,
                    &subject.key(),
                    &ctx.actor_id,
                )?;
                super::security::policy_change(
                    &tx,
                    &ctx,
                    deployment,
                    json!({"kind":"role","subject":subject.key(),"role":role}),
                )?;
                let result = read_policy(&tx, deployment)?;
                receipt(&tx, &ctx, deployment, key, &command, &result)?;
                result
            }
            AdminCommand::SetGrant {
                deployment,
                subject: who,
                grant,
                effective_principal,
                source_revision,
                present,
                expected_policy,
                key,
            } => {
                if let Some(result) = replay(&tx, &ctx, deployment, key, &command)? {
                    return Ok(result);
                }
                expected(&tx, deployment, *expected_policy)?;
                let subject = subject(&tx, who)?;
                let effective = effective_principal.as_deref().unwrap_or("");
                let source = source_revision.as_deref().unwrap_or("");
                match grant {
                    Grant::ActAs=>{
                        if !tx.prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND kind='service' AND (?2=0 OR disabled=0)")?.exists(params![effective, present])?
                            || !tx.prepare("SELECT 1 FROM authorization_sources WHERE deployment=?1 AND revision=?2")?.exists(params![deployment,source])? {return Err(auth::denied());}
                    },
                    Grant::Execute|Grant::StopAny=>if !effective.is_empty() || !source.is_empty() {return Err(auth::denied());},
                }
                if *present {
                    tx.execute(
                        "INSERT OR IGNORE INTO authorization_grants VALUES (?1,?2,?3,?4,?5)",
                        params![deployment, subject, grant.name(), effective, source],
                    )?;
                } else {
                    tx.execute("DELETE FROM authorization_grants WHERE deployment=?1 AND subject=?2 AND capability=?3 AND effective_principal=?4 AND source_revision=?5",params![deployment,subject,grant.name(),effective,source])?;
                }
                tx.execute(
                    "UPDATE authorization_policy SET revision=revision+1 WHERE deployment=?1",
                    [deployment],
                )?;
                audit(
                    &tx,
                    &ctx,
                    deployment,
                    *expected_policy,
                    "policy.grant",
                    key,
                    &subject,
                    &ctx.actor_id,
                )?;
                super::security::policy_change(
                    &tx,
                    &ctx,
                    deployment,
                    json!({"kind":"execution_grant","subject":subject,"capability":grant,"effective_principal_id":effective,"source_revision":source,"present":present}),
                )?;
                let result = read_policy(&tx, deployment)?;
                receipt(&tx, &ctx, deployment, key, &command, &result)?;
                result
            }
        };
        tx.commit()?;
        Ok(result)
    }
    pub(crate) fn authorized_command(&mut self, ctx: &Context, command: Command) -> Result<Value> {
        let deployment = command.deployment().map(str::to_owned);
        let result = self.authorized_inner(ctx, command);
        if result.is_err() {
            let _ = self.audit_denial(ctx, deployment.as_deref());
        }
        result
    }
    fn authorized_inner(&mut self, ctx: &Context, command: Command) -> Result<Value> {
        validate_context(&self.db, ctx)?;
        if matches!(command, Command::Projects {}) {
            let mut projects = Vec::new();
            for id in self
                .db
                .prepare("SELECT deployment FROM authorization_policy ORDER BY deployment")?
                .query_map([], |r| r.get::<_, String>(0))?
            {
                let id = id?;
                if is_owner(&self.db, ctx)? || role(&self.db, &ctx.actor_id, &id)? > 0 {
                    projects.push(project(&self.db, &id)?);
                }
            }
            return Ok(json!({"api_version":auth::VERSION,"projects":projects}));
        }
        let deployment = command.deployment().ok_or_else(auth::denied)?.to_owned();
        let rank = match &command {
            Command::SetRole { .. } => 3,
            Command::SaveSource { .. } => 2,
            Command::Projects {}
            | Command::Catalog { .. }
            | Command::Data { .. }
            | Command::Runtime { .. }
            | Command::Project { .. }
            | Command::Policy { .. }
            | Command::Sources { .. }
            | Command::Source { .. }
            | Command::AdmitExecution { .. }
            | Command::Executions { .. }
            | Command::Execution { .. }
            | Command::StopExecution { .. }
            | Command::Unavailable { .. } => 1,
        };
        require(&self.db, ctx, &deployment, rank)?;
        if matches!(command, Command::Unavailable { .. }) {
            return Err(auth::denied());
        }
        let tx = self.db.transaction()?;
        // Recheck action-specific permission before replaying a committed receipt.
        // A receipt cannot restore lost project, execution or service-use rights.
        match &command {
            Command::AdmitExecution {
                source_revision,
                effective_principal,
                ..
            } => {
                if !is_owner(&tx, ctx)?
                    && !granted(&tx, &ctx.actor_id, &deployment, Grant::Execute, "", "")?
                {
                    return Err(auth::denied());
                }
                if let Some(effective) = effective_principal
                    && effective != &ctx.actor_id
                {
                    if !tx.prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND kind='service' AND disabled=0")?.exists([effective])?
                        || !granted(&tx,&ctx.actor_id,&deployment,Grant::ActAs,effective,source_revision)?
                        || role(&tx,effective,&deployment)?==0 || !granted(&tx,effective,&deployment,Grant::Execute,"","")? {return Err(auth::denied());}
                }
            }
            Command::StopExecution { id, .. } | Command::Execution { id, .. } => {
                execution_access(&tx, ctx, &deployment, &execution(&tx, &deployment, id)?)?
            }
            _ => {}
        }
        if let Some((revision, key)) = command.mutation() {
            if let Some(result) = replay(&tx, ctx, &deployment, key, &command)? {
                return Ok(result);
            }
            expected(&tx, &deployment, revision)?;
        }
        let result=match &command {
            Command::Project{..}=>project(&tx,&deployment)?,
            Command::Policy{..}=>read_policy(&tx,&deployment)?,
            Command::SetRole{subject,role,expected_policy,key,..}=>{
                // Project administrators manage membership, never execution or act_as grants.
                set_role(&tx,&deployment,subject,role)?;
                audit(&tx,ctx,&deployment,*expected_policy,"policy.role",key,&subject.key(),&ctx.actor_id)?;
                super::security::policy_change(&tx,ctx,&deployment,json!({"kind":"role","subject":subject.key(),"role":role}))?;
                read_policy(&tx,&deployment)?
            },
            Command::Sources{..}=>{
                let sources=tx.prepare("SELECT h.asset,h.revision,s.kind FROM authorization_heads h JOIN authorization_sources s ON s.deployment=h.deployment AND s.revision=h.revision WHERE h.deployment=?1 ORDER BY h.asset")?.query_map([&deployment],|r|Ok(json!({"asset":r.get::<_,String>(0)?,"revision":r.get::<_,String>(1)?,"kind":r.get::<_,String>(2)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
                json!({"sources":sources,"policy_revision":policy(&tx,&deployment)?})
            },
            Command::Source{revision,..}=>tx.query_row("SELECT asset,kind,contents FROM authorization_sources WHERE deployment=?1 AND revision=?2",params![deployment,revision],|r|Ok(json!({"revision":revision,"asset":r.get::<_,String>(0)?,"kind":r.get::<_,String>(1)?,"contents":r.get::<_,String>(2)?}))).optional()?.ok_or_else(auth::denied)?,
            Command::SaveSource{asset,kind,contents,expected_head,expected_policy,key,..}=>{
                if asset.is_empty() || asset.len()>128 || !asset.bytes().all(|b|b.is_ascii_alphanumeric() || b"_- .".contains(&b)) || !matches!(kind.as_str(),"sql"|"notebook") || contents.len()>32768 {return Err(auth::denied());}
                if kind=="notebook" {let _:serde_json::Value=serde_json::from_str(contents).map_err(|_|auth::denied())?;}
                let head:Option<String>=tx.query_row("SELECT revision FROM authorization_heads WHERE deployment=?1 AND asset=?2",params![deployment,asset],|r|r.get(0)).optional()?;
                if &head!=expected_head {return Err(conflict("source head changed; refresh before editing"));}
                let revision=crate::project_apply::digest(&json!({"deployment":deployment,"asset":asset,"kind":kind,"contents":contents}))?;
                tx.execute("INSERT OR IGNORE INTO authorization_sources VALUES (?1,?2,?3,?4,?5,?6)",params![deployment,revision,asset,kind,contents,ctx.actor_id])?;
                tx.execute("INSERT INTO authorization_heads VALUES (?1,?2,?3) ON CONFLICT(deployment,asset) DO UPDATE SET revision=excluded.revision",params![deployment,asset,revision])?;
                audit(&tx,ctx,&deployment,*expected_policy,"source.save",key,&revision,&ctx.actor_id)?;
                json!({"revision":revision,"asset":asset,"policy_revision":expected_policy})
            },
            Command::AdmitExecution{source_revision,effective_principal,expected_policy,key,..}=>{
                if !tx.prepare("SELECT 1 FROM authorization_sources WHERE deployment=?1 AND revision=?2")?.exists(params![deployment,source_revision])? {return Err(auth::denied());}
                let effective=effective_principal.as_deref().unwrap_or(&ctx.actor_id);let id=identity::id();
                // This is durable admission intent only. Only the private
                // isolated execution adapter may consume it.
                tx.execute("INSERT INTO authorization_executions VALUES (?1,?2,?3,?4,?5,?6,'admitted')",params![id,deployment,ctx.actor_id,effective,source_revision,expected_policy])?;
                audit(&tx,ctx,&deployment,*expected_policy,"execution.admit",key,&id,effective)?;
                execution(&tx,&deployment,&id)?
            },
            Command::Execution{id,..}=>execution(&tx,&deployment,id)?,
            Command::Executions{..}=>{
                let all=is_owner(&tx,ctx)? || granted(&tx,&ctx.actor_id,&deployment,Grant::StopAny,"","")?;
                let ids=tx.prepare("SELECT id FROM authorization_executions WHERE deployment=?1 AND (?2 OR actor=?3) ORDER BY id")?.query_map(params![deployment,all,ctx.actor_id],|r|r.get::<_,String>(0))?.collect::<rusqlite::Result<Vec<_>>>()?;
                json!({"executions":ids.iter().map(|id|execution(&tx,&deployment,id)).collect::<Result<Vec<_>>>()?})
            },
            Command::StopExecution{id,expected_policy,key,..}=>{
                tx.execute("UPDATE authorization_executions SET state='cancelled' WHERE id=?1 AND deployment=?2",params![id,deployment])?;
                let result=execution(&tx,&deployment,id)?;
                audit(&tx,ctx,&deployment,*expected_policy,"execution.cancel",key,id,result["effective_principal_id"].as_str().ok_or_else(auth::denied)?)?;result
            },
            Command::Data{..}|Command::Projects {}|Command::Catalog{..}|Command::Runtime{..}|Command::Unavailable{..}=>return Err(auth::denied()),
        };
        if let Some((_, key)) = command.mutation() {
            receipt(&tx, ctx, &deployment, key, &command, &result)?;
        }
        tx.commit()?;
        Ok(result)
    }
}

#[cfg(test)]
#[path = "authorization_tests.rs"]
mod tests;
