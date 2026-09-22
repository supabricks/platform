use super::*;
use crate::identity::{
    self as iam, AdminCommand, AuthCommand, Channel, Context, denied, hash, now, oidc,
};
use serde_json::{Value, json};

fn audit(db: &Connection, actor: &str, effective: &str, action: &str, target: &str) -> Result<()> {
    db.execute("INSERT INTO identity_audit(at_ms,actor,effective_principal,action,target) VALUES (?1,?2,?3,?4,?5)",params![now(),actor,effective,action,target])?;
    Ok(())
}
fn owner(db: &Connection) -> Result<String> {
    Ok(db.query_row("SELECT local_owner FROM identity_realm", [], |r| r.get(0))?)
}
fn provider(db: &Connection, id: &str) -> Result<oidc::Config> {
    let config: String = db
        .query_row(
            "SELECT config FROM identity_providers WHERE id=?1",
            [id],
            |r| r.get(0),
        )
        .optional()?
        .ok_or_else(denied)?;
    Ok(serde_json::from_str(&config)?)
}
fn principal(db: &Connection, issuer: &str, subject: &str, label: &str) -> Result<String> {
    if issuer.is_empty() || issuer.len() > 2048 || subject.is_empty() || subject.len() > 1024 {
        return Err(denied());
    }
    iam::label(label)?;
    if let Some((id,disabled))=db.query_row("SELECT p.id,p.disabled FROM identity_subjects s JOIN identity_principals p ON p.id=s.principal WHERE s.issuer=?1 AND s.subject=?2",params![issuer,subject],|r|Ok((r.get::<_,String>(0)?,r.get::<_,bool>(1)?))).optional()? {
        if disabled {return Err(denied());}
        db.execute("UPDATE identity_principals SET label=?1 WHERE id=?2",params![label,id])?;
        return Ok(id);
    }
    let id = iam::id();
    db.execute(
        "INSERT INTO identity_principals(id,kind,label) VALUES (?1,'user',?2)",
        params![id, label],
    )?;
    db.execute(
        "INSERT INTO identity_subjects VALUES (?1,?2,?3)",
        params![issuer, subject, id],
    )?;
    Ok(id)
}
struct Grant<'a> {
    principal: &'a str,
    channel: Channel,
    scopes: Vec<String>,
    expires: i64,
    provider: Option<&'a str>,
    access_token: Option<&'a str>,
    subject: Option<&'a str>,
}
fn issue(db: &Connection, grant: Grant<'_>) -> Result<Value> {
    super::security::checkpoint_clock(db)?;
    db.execute(
        "DELETE FROM identity_sessions WHERE expires_ms<=?1",
        [now()],
    )?;
    let token = iam::secret()?;
    let csrf = iam::secret()?;
    let epoch: i64 = db.query_row("SELECT session_epoch FROM identity_realm", [], |r| r.get(0))?;
    db.execute("INSERT INTO identity_sessions(token_hash,principal,csrf_hash,channel,scopes,expires_ms,epoch,provider,access_token,subject) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",params![hash(&token),grant.principal,hash(&csrf),grant.channel.name(),serde_json::to_string(&grant.scopes)?,grant.expires,epoch,grant.provider,grant.access_token,grant.subject])?;
    Ok(
        json!({"api_version":iam::VERSION,"token":token,"csrf":csrf,"expires_ms":grant.expires,"principal_id":grant.principal,"channel":grant.channel}),
    )
}

pub(crate) type IdentityCommit = Box<dyn FnOnce(&mut Store) -> Result<Value> + Send>;
pub(crate) type IdentityJob = Box<dyn FnOnce() -> Result<IdentityCommit> + Send>;
fn identity_fence(db: &Connection, name: &str) -> Result<(i64, i64)> {
    Ok(db.query_row("SELECT p.revision,r.session_epoch FROM identity_providers p CROSS JOIN identity_realm r WHERE p.id=?1",[name],|r|Ok((r.get(0)?,r.get(1)?)))?)
}
fn check_identity_fence(db: &Connection, name: &str, expected: (i64, i64)) -> Result<()> {
    if identity_fence(db, name)? != expected {
        return Err(denied());
    }
    Ok(())
}

impl Store {
    /// Only called by the private operator control socket, never by an HTTP route.
    pub fn identity_admin(&mut self, command: AdminCommand) -> Result<Value> {
        let actor = owner(&self.db)?;
        self.identity_admin_as(&actor, command)
    }
    pub(super) fn identity_admin_as(
        &mut self,
        actor: &str,
        command: AdminCommand,
    ) -> Result<Value> {
        match &command {
            AdminCommand::AuditExport { after } => {
                return super::security::export(&self.db, *after);
            }
            AdminCommand::AuditAcknowledge {
                after,
                through,
                sha256,
            } => return super::security::acknowledge(&mut self.db, *after, *through, sha256),
            AdminCommand::RestoreStatus => return super::security::restore_status(&self.db),
            AdminCommand::RestoreReconcile {
                restore_id,
                realm_id,
            } => return super::security::reconcile(&mut self.db, restore_id, realm_id),
            _ => {}
        }
        let tx = self.db.transaction()?;
        let actor = actor.to_owned();
        let result = match command {
            AdminCommand::AuditExport { .. }
            | AdminCommand::AuditAcknowledge { .. }
            | AdminCommand::RestoreStatus
            | AdminCommand::RestoreReconcile { .. } => unreachable!(),
            AdminCommand::Status => {
                let realm: String =
                    tx.query_row("SELECT id FROM identity_realm", [], |r| r.get(0))?;
                let mut stmt = tx.prepare(
                    "SELECT id,kind,label,disabled FROM identity_principals ORDER BY id",
                )?;
                let principals=stmt.query_map([],|r|Ok(json!({"id":r.get::<_,String>(0)?,"kind":r.get::<_,String>(1)?,"label":r.get::<_,String>(2)?,"disabled":r.get::<_,bool>(3)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
                let bootstrap: Option<String> =
                    tx.query_row("SELECT bootstrap_principal FROM identity_realm", [], |r| {
                        r.get(0)
                    })?;
                let mut groups = tx.prepare("SELECT id,label FROM identity_groups ORDER BY id")?;
                let groups = groups
                    .query_map([], |r| {
                        Ok(json!({"id":r.get::<_,String>(0)?,"label":r.get::<_,String>(1)?}))
                    })?
                    .collect::<std::result::Result<Vec<_>, _>>()?;
                let mut memberships=tx.prepare("SELECT group_id,principal FROM identity_memberships ORDER BY group_id,principal")?;
                let memberships=memberships.query_map([],|r|Ok(json!({"group_id":r.get::<_,String>(0)?,"principal_id":r.get::<_,String>(1)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
                json!({"api_version":iam::VERSION,"realm_id":realm,"local_owner_id":owner(&tx)?,"bootstrap_principal_id":bootstrap,"principals":principals,"groups":groups,"memberships":memberships,"governed_ingress":false})
            }
            AdminCommand::Configure {
                provider: name,
                config,
            } => {
                iam::label(&name)?;
                config.validate()?;
                // Configuration changes invalidate pending logins and all provider sessions.
                tx.execute("DELETE FROM identity_logins WHERE provider=?1", [&name])?;
                tx.execute("DELETE FROM identity_sessions WHERE provider=?1", [&name])?;
                tx.execute("INSERT INTO identity_providers(id,config) VALUES (?1,?2) ON CONFLICT(id) DO UPDATE SET config=excluded.config,revision=identity_providers.revision+1",params![name,serde_json::to_string(&config)?])?;
                audit(&tx, &actor, &actor, "provider.configure", &name)?;
                json!({"configured":name})
            }
            AdminCommand::Bootstrap {
                issuer,
                subject,
                label,
            } => {
                let configured: bool = tx
                    .prepare(
                        "SELECT 1 FROM identity_providers WHERE json_extract(config,'$.issuer')=?1",
                    )?
                    .exists([&issuer])?;
                let existing: Option<String> =
                    tx.query_row("SELECT bootstrap_principal FROM identity_realm", [], |r| {
                        r.get(0)
                    })?;
                if !configured || existing.is_some() {
                    return Err(conflict(
                        "bootstrap requires a configured issuer and an unassigned realm administrator",
                    ));
                }
                let id = principal(&tx, &issuer, &subject, &label)?;
                tx.execute("UPDATE identity_realm SET bootstrap_principal=?1", [&id])?;
                audit(&tx, &actor, &actor, "realm.bootstrap", &id)?;
                json!({"principal_id":id})
            }
            AdminCommand::Disable {
                principal,
                disabled,
            } => {
                if principal == actor || principal == owner(&tx)? {
                    return Err(denied());
                }
                if tx.execute(
                    "UPDATE identity_principals SET disabled=?1 WHERE id=?2",
                    params![disabled, principal],
                )? != 1
                {
                    return Err(denied());
                }
                tx.execute(
                    "DELETE FROM identity_sessions WHERE principal=?1",
                    [&principal],
                )?;
                audit(
                    &tx,
                    &actor,
                    &actor,
                    if disabled {
                        "principal.disable"
                    } else {
                        "principal.enable"
                    },
                    &principal,
                )?;
                json!({"updated":true})
            }
            AdminCommand::Group { label } => {
                iam::label(&label)?;
                let id = iam::id();
                tx.execute(
                    "INSERT INTO identity_groups VALUES (?1,?2)",
                    params![id, label],
                )?;
                audit(&tx, &actor, &actor, "group.create", &id)?;
                json!({"group_id":id})
            }
            AdminCommand::Membership {
                group,
                principal,
                present,
            } => {
                if present {
                    tx.execute(
                        "INSERT OR IGNORE INTO identity_memberships VALUES (?1,?2)",
                        params![group, principal],
                    )?;
                } else {
                    tx.execute(
                        "DELETE FROM identity_memberships WHERE group_id=?1 AND principal=?2",
                        params![group, principal],
                    )?;
                }
                audit(
                    &tx,
                    &actor,
                    &actor,
                    if present { "group.add" } else { "group.remove" },
                    &format!("{group}/{principal}"),
                )?;
                json!({"updated":true})
            }
            AdminCommand::Service { label } => {
                iam::label(&label)?;
                let id = iam::id();
                tx.execute(
                    "INSERT INTO identity_principals(id,kind,label) VALUES (?1,'service',?2)",
                    params![id, label],
                )?;
                audit(&tx, &actor, &actor, "service.create", &id)?;
                json!({"principal_id":id})
            }
            AdminCommand::IssueService {
                principal,
                scopes,
                ttl_seconds,
            } => {
                if (scopes.is_empty() || scopes.len()>2 || !scopes.iter().any(|s|s==iam::SELF_SCOPE) || scopes.iter().any(|s|s!=iam::SELF_SCOPE && s!=crate::authorization::CONTROL_SCOPE) || scopes.iter().collect::<std::collections::HashSet<_>>().len()!=scopes.len()) || !(1..=3600).contains(&ttl_seconds) || !tx.prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND kind='service' AND disabled=0")?.exists([&principal])? {return Err(denied());}
                let result = issue(
                    &tx,
                    Grant {
                        principal: &principal,
                        channel: Channel::Service,
                        scopes,
                        expires: now() + i64::from(ttl_seconds) * 1000,
                        provider: None,
                        access_token: None,
                        subject: None,
                    },
                )?;
                audit(&tx, &actor, &actor, "service.credential.issue", &principal)?;
                result
            }
            AdminCommand::Revoke { principal } => {
                tx.execute(
                    "DELETE FROM identity_sessions WHERE principal=?1",
                    [&principal],
                )?;
                audit(&tx, &actor, &actor, "sessions.revoke", &principal)?;
                json!({"revoked":true})
            }
            AdminCommand::RotateSessions => {
                tx.execute(
                    "UPDATE identity_realm SET session_epoch=session_epoch+1",
                    [],
                )?;
                tx.execute("DELETE FROM identity_sessions", [])?;
                tx.execute("DELETE FROM identity_logins", [])?;
                audit(&tx, &actor, &actor, "sessions.rotate", &actor)?;
                json!({"rotated":true})
            }
            AdminCommand::Audit { after } => {
                let mut stmt=tx.prepare("SELECT sequence,at_ms,actor,effective_principal,action,target FROM identity_audit WHERE sequence>?1 ORDER BY sequence LIMIT 200")?;
                json!({"events":stmt.query_map([after],|r|Ok(json!({"sequence":r.get::<_,i64>(0)?,"at_ms":r.get::<_,i64>(1)?,"actor_id":r.get::<_,String>(2)?,"effective_principal_id":r.get::<_,String>(3)?,"action":r.get::<_,String>(4)?,"target":r.get::<_,String>(5)?})))?.collect::<std::result::Result<Vec<_>,_>>()?})
            }
        };
        tx.commit()?;
        Ok(result)
    }
    pub fn identity_auth(&mut self, command: AuthCommand) -> Result<Value> {
        if let AuthCommand::Logout {
            token,
            channel,
            csrf,
        } = command
        {
            // Local logout must still work while the IdP is unavailable.
            let context = self.identity_context_local(&token, channel, csrf.as_deref())?;
            let tx = self.db.transaction()?;
            tx.execute(
                "DELETE FROM identity_sessions WHERE token_hash=?1",
                [hash(&token)],
            )?;
            audit(
                &tx,
                &context.actor_id,
                &context.effective_principal_id,
                "session.logout",
                &context.actor_id,
            )?;
            tx.commit()?;
            Ok(json!({"logged_out":true}))
        } else {
            let job = self.identity_job(command)?;
            job()?(self)
        }
    }
    /// Snapshot/consume on the writer, perform OIDC I/O on a bounded worker,
    /// then revalidate the snapshot before committing on the writer.
    pub(crate) fn identity_job(&mut self, command: AuthCommand) -> Result<IdentityJob> {
        super::security::admission(&self.db)?;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        let authoritative_until = now() + super::security::FRESH_MS;
        match command {
            AuthCommand::Begin {
                provider: name,
                redirect,
                binding,
                channel,
            } => {
                if channel == Channel::Service || binding.len() != 64 {
                    return Err(denied());
                }
                let config = provider(&self.db, &name)?;
                config.redirect(&redirect)?;
                let fence = identity_fence(&self.db, &name)?;
                Ok(Box::new(move || {
                    let login = oidc::begin(&config, &redirect)?;
                    Ok(Box::new(move |store: &mut Store| {
                        if std::time::Instant::now() >= deadline {
                            return Err(denied());
                        }
                        super::security::admission(&store.db)?;
                        check_identity_fence(&store.db, &name, fence)?;
                        let tx = store.db.transaction()?;
                        tx.execute("DELETE FROM identity_logins WHERE expires_ms<=?1", [now()])?;
                        let count: i64 =
                            tx.query_row("SELECT count(*) FROM identity_logins", [], |r| r.get(0))?;
                        if count >= 128 {
                            return Err(denied());
                        }
                        tx.execute(
                            "INSERT INTO identity_logins VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
                            params![
                                hash(&login.state),
                                hash(&binding),
                                name,
                                redirect,
                                login.nonce,
                                login.verifier,
                                channel.name(),
                                now() + 300_000
                            ],
                        )?;
                        tx.commit()?;
                        Ok(json!({"api_version":iam::VERSION,"authorization_url":login.url}))
                    }))
                }))
            }
            AuthCommand::Complete {
                state,
                code,
                binding,
                redirect,
                channel,
            } => {
                let pending:Option<(String,String,String,String)>=self.db.query_row("DELETE FROM identity_logins WHERE state_hash=?1 AND binding_hash=?2 AND redirect=?3 AND channel=?4 AND expires_ms>?5 RETURNING provider,nonce,verifier,redirect",params![hash(&state),hash(&binding),redirect,channel.name(),now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
                let (name, nonce, verifier, redirect) = pending.ok_or_else(denied)?;
                let config = provider(&self.db, &name)?;
                let fence = identity_fence(&self.db, &name)?;
                Ok(Box::new(move || {
                    let verified = oidc::complete(&config, &redirect, code, nonce, verifier)?;
                    Ok(Box::new(move |store: &mut Store| {
                        if std::time::Instant::now() >= deadline {
                            return Err(denied());
                        }
                        super::security::admission(&store.db)?;
                        check_identity_fence(&store.db, &name, fence)?;
                        if verified.expires_ms <= now() {
                            return Err(denied());
                        }
                        let tx = store.db.transaction()?;
                        let id =
                            principal(&tx, &config.issuer, &verified.subject, &verified.label)?;
                        let result = issue(
                            &tx,
                            Grant {
                                principal: &id,
                                channel,
                                scopes: vec![
                                    iam::SELF_SCOPE.into(),
                                    crate::authorization::CONTROL_SCOPE.into(),
                                ],
                                expires: verified.expires_ms,
                                provider: Some(&name),
                                access_token: Some(&verified.access_token),
                                subject: Some(&verified.subject),
                            },
                        )?;
                        audit(&tx, &id, &id, "session.login", &id)?;
                        tx.commit()?;
                        Ok(result)
                    }))
                }))
            }
            AuthCommand::Authenticate {
                token,
                channel,
                csrf,
            } => {
                let context =
                    self.identity_context_local(&token, channel.clone(), csrf.as_deref())?;
                let upstream = if context.channel == Channel::Service {
                    None
                } else {
                    let (name,access,subject):(String,String,String)=self.db.query_row("SELECT provider,access_token,subject FROM identity_sessions WHERE token_hash=?1",[hash(&token)],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
                    let config = provider(&self.db, &name)?;
                    let fence = identity_fence(&self.db, &name)?;
                    Some((name, config, access, subject, fence))
                };
                Ok(Box::new(move || {
                    let active = upstream
                        .as_ref()
                        .map(|(_, config, access, subject, _)| {
                            oidc::active(config, access, subject)
                        })
                        .transpose();
                    Ok(Box::new(move |store: &mut Store| {
                        if active.is_err() {
                            // Negative provider answers/outages cannot leave a reusable
                            // authority. Local logout remains available.
                            store.db.execute(
                                "UPDATE identity_sessions SET authoritative_until_ms=0 WHERE token_hash=?1",
                                [hash(&token)],
                            )?;
                            let _ = store.audit_denial(&context, None);
                            return Err(denied());
                        }
                        if std::time::Instant::now() >= deadline {
                            return Err(denied());
                        }
                        super::security::admission(&store.db)?;
                        if let Some((name, _, _, _, fence)) = upstream {
                            check_identity_fence(&store.db, &name, fence)?;
                        }
                        super::security::checkpoint_clock(&store.db)?;
                        store.db.execute("UPDATE identity_sessions SET authoritative_until_ms=?2 WHERE token_hash=?1", params![hash(&token), authoritative_until])?;
                        Ok(serde_json::to_value(store.identity_context_local(
                            &token,
                            channel,
                            csrf.as_deref(),
                        )?)?)
                    }))
                }))
            }
            AuthCommand::Logout { .. } => Err(denied()),
        }
    }
    fn identity_context_local(
        &self,
        token: &str,
        channel: Channel,
        csrf: Option<&str>,
    ) -> Result<Context> {
        super::security::admission(&self.db)?;
        if token.len() != 64 {
            return Err(denied());
        }
        let record:Option<(String,String,String,i64,String)>=self.db.query_row("SELECT r.id,s.principal,s.scopes,s.expires_ms,s.csrf_hash FROM identity_sessions s JOIN identity_principals p ON p.id=s.principal JOIN identity_realm r ON r.singleton=1 WHERE s.token_hash=?1 AND s.channel=?2 AND s.expires_ms>?3 AND s.epoch=r.session_epoch AND p.disabled=0",params![hash(token),channel.name(),now()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
        let (realm, id, scopes, expires, csrf_hash) = record.ok_or_else(denied)?;
        if channel == Channel::Browser && csrf.is_none_or(|c| hash(c) != csrf_hash) {
            return Err(denied());
        }
        Ok(Context {
            api_version: iam::VERSION,
            realm_id: realm,
            actor_id: id.clone(),
            effective_principal_id: id,
            channel,
            scopes: serde_json::from_str(&scopes)?,
            expires_ms: expires,
        })
    }
    pub fn identity_context(
        &self,
        token: &str,
        channel: Channel,
        csrf: Option<&str>,
    ) -> Result<Context> {
        let context = self.identity_context_local(token, channel, csrf)?;
        if context.channel != Channel::Service {
            let (name, access, subject): (String, String, String) = self.db.query_row(
                "SELECT provider,access_token,subject FROM identity_sessions WHERE token_hash=?1",
                [hash(token)],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )?;
            oidc::active(&provider(&self.db, &name)?, &access, &subject)?;
        }
        Ok(context)
    }
}

#[cfg(test)]
#[path = "identity_tests.rs"]
mod tests;
