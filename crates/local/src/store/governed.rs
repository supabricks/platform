use super::authorization as a;
use super::*;
use crate::{
    authorization::{AdminCommand, Subject},
    governed::{Capability, Command, denied, postgres},
    identity::{self, Context},
};
use serde_json::{Value, json};
fn branch(db: &Connection, deployment: &str, id: &str) -> Result<()> {
    if !db.prepare("SELECT 1 FROM branches b JOIN deployments d ON d.runtime_project_id=b.project_id WHERE b.id=?1 AND d.id=?2 AND b.expired=0 AND b.desired!='deleted'")?.exists(params![id,deployment])? {return Err(denied());}
    Ok(())
}
fn grant(
    db: &Connection,
    ctx: &Context,
    deployment: &str,
    id: &str,
    cap: Capability,
) -> Result<()> {
    a::require(db, ctx, deployment, 1)?;
    branch(db, deployment, id)?;
    for subject in a::subjects(db, &ctx.actor_id)? {
        if db.prepare("SELECT 1 FROM data_grants WHERE deployment=?1 AND branch=?2 AND subject=?3 AND capability=?4")?.exists(params![deployment,id,subject,cap.name()])? {return Ok(());}
    }
    Err(denied())
}
#[allow(clippy::too_many_arguments)]
pub(super) fn set_grant(
    db: &Connection,
    ctx: &Context,
    command: &AdminCommand,
    deployment: &str,
    id: &str,
    who: &Subject,
    cap: Capability,
    present: bool,
    expected: i64,
    key: &str,
) -> Result<Value> {
    branch(db, deployment, id)?;
    if let Some(result) = a::replay(db, ctx, deployment, key, command)? {
        return Ok(result);
    }
    a::expected(db, deployment, expected)?;
    let subject = a::subject(db, who)?;
    if present {
        db.execute(
            "INSERT OR IGNORE INTO data_grants VALUES (?1,?2,?3,?4)",
            params![deployment, id, subject, cap.name()],
        )?;
    } else {
        db.execute("DELETE FROM data_grants WHERE deployment=?1 AND branch=?2 AND subject=?3 AND capability=?4",params![deployment,id,subject,cap.name()])?;
    }
    db.execute(
        "UPDATE authorization_policy SET revision=revision+1 WHERE deployment=?1",
        [deployment],
    )?;
    a::audit(
        db,
        ctx,
        deployment,
        expected,
        "data.grant",
        key,
        id,
        &ctx.actor_id,
    )?;
    let result = json!({"policy_revision":a::policy(db,deployment)?,"branch":id,"capability":cap,"present":present});
    a::receipt(db, ctx, deployment, key, command, &result)?;
    Ok(result)
}
fn session(db: &Connection, ctx: &Context, hash: &str) -> Result<()> {
    a::validate_context(db, ctx)?;
    if !db.prepare("SELECT 1 FROM identity_sessions s JOIN identity_realm r ON r.session_epoch=s.epoch WHERE s.token_hash=?1 AND s.principal=?2 AND s.expires_ms>?3")?.exists(params![hash,ctx.actor_id,identity::now()])? {return Err(denied());}
    Ok(())
}
impl Store {
    pub(crate) fn governed_branch(&self, id: BranchId) -> Result<bool> {
        Ok(self.db.prepare("SELECT 1 FROM governed_branches WHERE branch=?1 UNION SELECT 1 FROM operations WHERE branch_id=?1 AND request_key LIKE 'governed:%' AND json_extract(request,'$.kind')='branch_from'")?.exists([id.to_string()])?)
    }
    pub(crate) fn governed_clone_ready(&mut self, id: BranchId) -> Result<()> {
        let operation:Option<String>=self.db.query_row("SELECT id FROM operations WHERE branch_id=?1 AND request_key LIKE 'governed:%' AND json_extract(request,'$.kind')='branch_from'",[id.to_string()],|r|r.get(0)).optional()?;
        if let Some(operation) = operation {
            let state: Option<String> = self
                .db
                .query_row(
                    "SELECT state FROM governed_branches WHERE branch=?1",
                    [id.to_string()],
                    |r| r.get(0),
                )
                .optional()?;
            if state.as_deref() == Some("ready") {
                return Ok(());
            }
            let source: String = self.db.query_row(
                "SELECT id FROM data_operations WHERE result_json->>'$.clone_id'=?1",
                [operation],
                |r| r.get(0),
            )?;
            let (ctx, deployment, branch) = self.data_live(&source)?;
            grant(&self.db, &ctx, &deployment, &branch, Capability::Receive)?;
            self.db.execute(
                "UPDATE governed_branches SET state='ready' WHERE branch=?1",
                [id.to_string()],
            )?;
        }
        Ok(())
    }
    pub(crate) fn governed_export(&self, child: BranchId) -> Result<bool> {
        Ok(self
            .db
            .prepare(
                "SELECT 1 FROM operations WHERE branch_id=?1 AND request_key LIKE 'governed:%'",
            )?
            .exists([child.to_string()])?)
    }
    pub(crate) fn recover_data(&mut self) -> Result<()> {
        self.db.execute("UPDATE data_operations SET state=CASE WHEN state='committing' THEN 'uncertain' ELSE 'interrupted' END,result_json=NULL WHERE state IN ('preparing','committing')",[])?;
        Ok(())
    }
    fn data_live(&self, id: &str) -> Result<(Context, String, String)> {
        let(deployment,branch,context,hash,policy,revision,cap,state):(String,String,String,String,i64,i64,String,String)=self.db.query_row("SELECT deployment,branch,context_json,token_hash,policy_revision,branch_revision,capability,state FROM data_operations WHERE id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?)))?;
        if matches!(state.as_str(), "failed" | "interrupted" | "uncertain") {
            return Err(denied());
        }
        let ctx: Context = serde_json::from_str(&context)?;
        session(&self.db, &ctx, &hash)?;
        a::expected(&self.db, &deployment, policy)?;
        let cap: Capability = serde_json::from_value(json!(cap))?;
        grant(&self.db, &ctx, &deployment, &branch, cap)?;
        let b = self.branch(branch.parse().map_err(|_| denied())?)?;
        if b.revision != revision {
            return Err(denied());
        }
        Ok((ctx, deployment, branch))
    }
    /// Every private exporter tick and final publication commit consults the
    /// original actor's session and policy, even through an operator retry.
    pub(crate) fn data_export_live(&self, id: OperationId, sharing: bool) -> Result<()> {
        let admitted: Option<String> = self
            .db
            .query_row(
                "SELECT id FROM data_operations WHERE result_json->>'$.export_id'=?1",
                [id.to_string()],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(id) = admitted {
            let (ctx, d, b) = self.data_live(&id)?;
            if sharing {
                grant(&self.db, &ctx, &d, &b, Capability::Share)?;
            }
        } else {
            // A crash between durable native submission and mapping may leave
            // a private child; it must never become an ungoverned exporter.
            let guarded = self
                .db
                .prepare("SELECT 1 FROM operations WHERE id=?1 AND request_key LIKE 'governed:%'")?
                .exists([id.to_string()])?;
            if guarded {
                return Err(denied());
            }
        }
        Ok(())
    }
    pub(crate) fn data_job(
        &mut self,
        ctx: Context,
        hash: String,
        deployment: String,
        command: Command,
    ) -> Result<super::identity::IdentityJob> {
        session(&self.db, &ctx, &hash)?;
        a::require(&self.db, &ctx, &deployment, 1)?;
        if let Command::Find { key } = &command {
            crate::authorization::key(key)?;
            let operation:String=self.db.query_row("SELECT id FROM data_operations WHERE deployment=?1 AND actor=?2 AND request_key=?3",params![deployment,ctx.actor_id,key],|r|r.get(0)).optional()?.ok_or_else(denied)?;
            return self.data_job(ctx, hash, deployment, Command::Status { operation });
        }
        if let Command::Status { operation } = &command {
            let (actor, d): (String, String) = self.db.query_row(
                "SELECT actor,deployment FROM data_operations WHERE id=?1",
                [operation],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            if actor != ctx.actor_id || d != deployment {
                return Err(denied());
            }
            let (state, result): (String, Option<String>) = self.db.query_row(
                "SELECT state,result_json FROM data_operations WHERE id=?1",
                [operation],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            let terminal_error = matches!(state.as_str(), "failed" | "interrupted" | "uncertain");
            if !terminal_error {
                self.data_live(operation)?;
            }
            let value = json!({"id":operation,"state":state,"result":if terminal_error{None}else{result.map(|s|serde_json::from_str::<Value>(&s)).transpose()?}});
            let operation = operation.clone();
            return Ok(Box::new(move || {
                Ok(Box::new(move |store| {
                    session(&store.db, &ctx, &hash)?;
                    a::require(&store.db, &ctx, &deployment, 1)?;
                    if !terminal_error {
                        store.data_live(&operation)?;
                    }
                    Ok(value)
                }))
            }));
        }
        if let Command::Publish {
            export,
            expected_policy,
        } = command
        {
            a::expected(&self.db, &deployment, expected_policy)?;
            let id: OperationId = export.parse().map_err(|_| denied())?;
            let (actor, d): (String, String) = self.db.query_row(
                "SELECT actor,deployment FROM data_operations WHERE result_json->>'$.export_id'=?1",
                [&export],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            if actor != ctx.actor_id || d != deployment {
                return Err(denied());
            }
            self.data_export_live(id, true)?;
            let e = self.export(id)?;
            a::audit(
                &self.db,
                &ctx,
                &deployment,
                expected_policy,
                "data.publish",
                &export,
                &export,
                &ctx.actor_id,
            )?;
            let value = serde_json::to_value(self.publish_export(e.project_id, id)?)?;
            return Ok(Box::new(move || {
                Ok(Box::new(move |store| {
                    session(&store.db, &ctx, &hash)?;
                    store.data_export_live(id, true)?;
                    Ok(value)
                }))
            }));
        }
        let (b, cap, expected, key) = match &command {
            Command::ExportData {
                branch,
                expected_policy,
                key,
                ..
            } => (
                branch.clone(),
                Capability::CopySource,
                *expected_policy,
                key.clone(),
            ),
            Command::Import {
                branch,
                expected_policy,
                key,
                ..
            } => (
                branch.clone(),
                Capability::Receive,
                *expected_policy,
                key.clone(),
            ),
            Command::Sql {
                branch,
                capability,
                expected_policy,
                key,
                ..
            } => (branch.clone(), *capability, *expected_policy, key.clone()),
            Command::Clone {
                branch,
                expected_policy,
                key,
                ..
            }
            | Command::Export {
                branch,
                expected_policy,
                key,
            } => (
                branch.clone(),
                Capability::CopySource,
                *expected_policy,
                key.clone(),
            ),
            _ => return Err(denied()),
        };
        grant(&self.db, &ctx, &deployment, &b, cap)?;
        if matches!(command, Command::Import { .. }) {
            grant(&self.db, &ctx, &deployment, &b, Capability::Ddl)?;
        }
        if matches!(command, Command::Clone { .. }) {
            grant(&self.db, &ctx, &deployment, &b, Capability::Receive)?;
        }
        a::expected(&self.db, &deployment, expected)?;
        crate::authorization::key(&key)?;
        if self
            .db
            .prepare(
                "SELECT 1 FROM data_operations WHERE deployment=?1 AND actor=?2 AND request_key=?3",
            )?
            .exists(params![deployment, ctx.actor_id, key])?
        {
            return Err(conflict(
                "data request key already used; inspect its operation instead of replaying SQL",
            ));
        }
        if self.db.prepare("SELECT 1 FROM data_operations WHERE branch=?1 AND state IN ('preparing','committing')")?.exists([&b])? {return Err(conflict("branch has a governed transaction in flight"));}
        if self
            .db
            .query_row("SELECT count(*) FROM data_operations", [], |r| {
                r.get::<_, i64>(0)
            })?
            >= 512
        {
            return Err(conflict("governed data journal limit reached"));
        }
        let branch = self.branch(b.parse().map_err(|_| denied())?)?;
        self.accepting_work(branch.branch.id)?;
        if branch.revision != branch.observed_revision
            || branch.endpoint.desired_state != DesiredState::Running
        {
            return Err(denied());
        }
        self.governed_clone_ready(branch.branch.id)?;
        let target = postgres::Target {
            port: branch.ports.ok_or_else(denied)?.sql,
            password: self.endpoint_password(branch.endpoint.id)?,
        };
        let id = identity::id();
        let tx = self.db.transaction()?;
        a::audit(
            &tx,
            &ctx,
            &deployment,
            expected,
            "data.prepare",
            &key,
            &id,
            &ctx.actor_id,
        )?;
        tx.execute("INSERT INTO data_operations VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,'preparing',NULL)",params![id,deployment,b,ctx.actor_id,serde_json::to_string(&ctx)?,hash,expected,branch.revision,cap.name(),key,crate::project_apply::digest(&command)?])?;
        if matches!(
            &command,
            Command::Sql { .. } | Command::Import { .. } | Command::ExportData { .. }
        ) {
            tx.execute("INSERT INTO governed_branches VALUES (?1,'quarantined') ON CONFLICT(branch) DO NOTHING",[&b])?;
        }
        tx.commit()?;
        let definition: String = self.db.query_row(
            "SELECT definition_id FROM deployments WHERE id=?1",
            [&deployment],
            |r| r.get(0),
        )?;
        let source = crate::projects::data::Provenance {
            definition_id: definition.parse().map_err(|_| denied())?,
            source_sha256: crate::project_apply::digest(&command)?,
            runtime_project_id: branch.branch.project_id,
            branch_id: branch.branch.id,
            branch_revision: branch.revision,
            snapshot: String::new(),
        };
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        Ok(Box::new(move || {
            let prepared = match &command {
                Command::ExportData { selection, .. } => postgres::prepare_export(
                    target,
                    ctx.actor_id.clone(),
                    selection.clone(),
                    source,
                    ctx.expires_ms,
                )
                .map(Some),
                Command::Import { archive_hex, .. } => postgres::prepare_import(
                    target,
                    ctx.actor_id.clone(),
                    archive_hex.clone(),
                    ctx.expires_ms,
                )
                .map(Some),
                Command::Sql { sql, .. } => postgres::prepare(
                    target,
                    ctx.actor_id.clone(),
                    cap,
                    sql.clone(),
                    ctx.expires_ms,
                )
                .map(Some),
                Command::Clone { .. } | Command::Export { .. } => {
                    postgres::inspect(target).map(|_| None)
                }
                _ => Err(denied()),
            };
            Ok(Box::new(move |store| {
                let result = (|| {
                    if std::time::Instant::now() >= deadline {
                        return Err(denied());
                    }
                    store.data_live(&id)?;
                    session(&store.db, &ctx, &hash)?;
                    let pending = prepared?;
                    a::audit(
                        &store.db,
                        &ctx,
                        &deployment,
                        expected,
                        "data.commit",
                        &key,
                        &id,
                        &ctx.actor_id,
                    )?;
                    store.db.execute(
                        "UPDATE data_operations SET state='committing' WHERE id=?1",
                        [&id],
                    )?;
                    let value = if let Some(pending) = pending {
                        let value = pending.commit()?;
                        store.db.execute(
                            "UPDATE governed_branches SET state='ready' WHERE branch=?1",
                            [&b],
                        )?;
                        value
                    } else {
                        let listeners = (0..3)
                            .map(|_| std::net::TcpListener::bind("127.0.0.1:0"))
                            .collect::<std::io::Result<Vec<_>>>()?;
                        let ports = crate::operations::Ports {
                            sql: listeners[0].local_addr()?.port(),
                            external_http: listeners[1].local_addr()?.port(),
                            internal_http: listeners[2].local_addr()?.port(),
                        };
                        if let Command::Clone { name, .. } = &command {
                            grant(&store.db, &ctx, &deployment, &b, Capability::Receive)?;
                            let operation = store.submit(
                                branch.branch.project_id,
                                &format!("governed:{id}"),
                                crate::operations::Mutation::BranchFrom {
                                    name: name.clone(),
                                    parent_id: branch.branch.id,
                                    ports,
                                    point: Default::default(),
                                    timeout_ms: 90000,
                                },
                            )?;
                            store.db.execute(
                                "INSERT INTO governed_branches VALUES (?1,'quarantined')",
                                [operation.branch_id.to_string()],
                            )?;
                            json!({"clone_id":operation.id,"branch_id":operation.branch_id})
                        } else {
                            let operation = store.submit(
                                branch.branch.project_id,
                                &format!("governed:{id}"),
                                crate::operations::Mutation::Export {
                                    parent_id: branch.branch.id,
                                    ports,
                                    limits: Default::default(),
                                },
                            )?;
                            json!({"export_id":operation.id})
                        }
                    };
                    store.db.execute(
                        "UPDATE data_operations SET state='complete',result_json=?2 WHERE id=?1",
                        params![id, value.to_string()],
                    )?;
                    Ok(json!({"id":id,"result":value}))
                })();
                if result.is_err() {
                    store.db.execute(
                        "UPDATE data_operations SET state=CASE WHEN state='committing' THEN 'uncertain' ELSE 'failed' END,result_json=NULL WHERE id=?1",
                        [&id],
                    )?;
                }
                result
            }))
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        authorization::{AdminCommand, Role},
        identity::{AdminCommand as I, AuthCommand, Channel},
        operations::{Mutation, Ports},
    };
    struct F {
        _dir: tempfile::TempDir,
        store: Store,
        ctx: Context,
        hash: String,
        deployment: String,
        branch: String,
    }
    impl F {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let mut store = Store::open(&dir.path().join("state")).unwrap();
            let project = ProjectId::new();
            store
                .register_project(&ProjectConfig {
                    id: project,
                    name: "governed".into(),
                    format_version: 1,
                })
                .unwrap();
            let deployment: String = store
                .db
                .query_row("SELECT id FROM deployments", [], |r| r.get(0))
                .unwrap();
            let op = store
                .submit(
                    project,
                    "branch",
                    Mutation::CreateDatabase {
                        name: "main".into(),
                        ports: Ports {
                            sql: 18001,
                            external_http: 18002,
                            internal_http: 18003,
                        },
                    },
                )
                .unwrap();
            store
                .db
                .execute("UPDATE branches SET observed_revision=revision", [])
                .unwrap();
            let principal = store
                .identity_admin(I::Service {
                    label: "reader".into(),
                })
                .unwrap()["principal_id"]
                .as_str()
                .unwrap()
                .to_owned();
            let token = store
                .identity_admin(I::IssueService {
                    principal: principal.clone(),
                    scopes: vec![
                        identity::SELF_SCOPE.into(),
                        crate::authorization::CONTROL_SCOPE.into(),
                    ],
                    ttl_seconds: 3600,
                })
                .unwrap()["token"]
                .as_str()
                .unwrap()
                .to_owned();
            let ctx: Context = serde_json::from_value(
                store
                    .identity_auth(AuthCommand::Authenticate {
                        token: token.clone(),
                        channel: Channel::Service,
                        csrf: None,
                    })
                    .unwrap(),
            )
            .unwrap();
            store
                .authorization_admin(AdminCommand::SetRole {
                    deployment: deployment.clone(),
                    subject: Subject::Principal(principal),
                    role: Some(Role::Administrator),
                    expected_policy: a::policy(&store.db, &deployment).unwrap(),
                    key: identity::id(),
                })
                .unwrap();
            Self {
                _dir: dir,
                store,
                ctx,
                hash: identity::hash(&token),
                deployment,
                branch: op.branch_id.to_string(),
            }
        }
        fn grant(&mut self, cap: Capability, present: bool) {
            self.store
                .authorization_admin(AdminCommand::SetDataGrant {
                    deployment: self.deployment.clone(),
                    branch: self.branch.clone(),
                    subject: Subject::Principal(self.ctx.actor_id.clone()),
                    capability: cap,
                    present,
                    expected_policy: a::policy(&self.store.db, &self.deployment).unwrap(),
                    key: identity::id(),
                })
                .unwrap();
        }
        fn job(&mut self) -> Result<super::super::identity::IdentityJob> {
            let command = Command::Sql {
                branch: self.branch.clone(),
                capability: Capability::Read,
                sql: "SELECT 1".into(),
                expected_policy: a::policy(&self.store.db, &self.deployment)?,
                key: identity::id(),
            };
            self.store.data_job(
                self.ctx.clone(),
                self.hash.clone(),
                self.deployment.clone(),
                command,
            )
        }
    }
    #[test]
    fn project_control_never_implies_data_and_branch_grants_are_separate() {
        let mut f = F::new();
        assert!(f.job().is_err());
        f.grant(Capability::Read, true);
        assert!(
            grant(
                &f.store.db,
                &f.ctx,
                &f.deployment,
                &f.branch,
                Capability::Read
            )
            .is_ok()
        );
        for cap in [
            Capability::Write,
            Capability::Ddl,
            Capability::CopySource,
            Capability::Receive,
            Capability::Share,
        ] {
            assert!(grant(&f.store.db, &f.ctx, &f.deployment, &f.branch, cap).is_err());
        }
        assert!(
            grant(
                &f.store.db,
                &f.ctx,
                &f.deployment,
                &identity::id(),
                Capability::Read
            )
            .is_err()
        );
        let _pending = f.job().unwrap();
        assert!(f.job().is_err());
        let id: String = f
            .store
            .db
            .query_row("SELECT id FROM data_operations", [], |r| r.get(0))
            .unwrap();
        assert!(f.store.data_live(&id).is_ok());
        f.grant(Capability::Read, false);
        assert!(f.store.data_live(&id).is_err());
    }
    #[test]
    fn audit_failure_has_no_data_effect_and_restart_never_replays_sql() {
        let mut f = F::new();
        f.grant(Capability::Read, true);
        f.store.db.execute_batch("CREATE TRIGGER fail_data_audit BEFORE INSERT ON authorization_audit BEGIN SELECT RAISE(ABORT,'full'); END;").unwrap();
        assert!(f.job().is_err());
        assert_eq!(
            f.store
                .db
                .query_row("SELECT count(*) FROM data_operations", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        f.store
            .db
            .execute_batch("DROP TRIGGER fail_data_audit")
            .unwrap();
        let _pending = f.job().unwrap();
        f.store.recover_data().unwrap();
        assert_eq!(
            f.store
                .db
                .query_row("SELECT state FROM data_operations", [], |r| r
                    .get::<_, String>(0))
                .unwrap(),
            "interrupted"
        );
    }
    #[test]
    fn session_disable_and_policy_fence_pending_results() {
        for mode in 0..4 {
            let mut f = F::new();
            f.grant(Capability::Read, true);
            let _pending = f.job().unwrap();
            let id: String = f
                .store
                .db
                .query_row("SELECT id FROM data_operations", [], |r| r.get(0))
                .unwrap();
            match mode {
                0 => {
                    f.store
                        .db
                        .execute("DELETE FROM identity_sessions", [])
                        .unwrap();
                }
                1 => {
                    f.store
                        .db
                        .execute(
                            "UPDATE identity_principals SET disabled=1 WHERE id=?1",
                            [&f.ctx.actor_id],
                        )
                        .unwrap();
                }
                2 => {
                    f.store
                        .db
                        .execute("UPDATE authorization_policy SET revision=revision+1", [])
                        .unwrap();
                }
                _ => {
                    f.store
                        .db
                        .execute("UPDATE branches SET revision=revision+1", [])
                        .unwrap();
                }
            }
            assert!(f.store.data_live(&id).is_err());
        }
    }
}
