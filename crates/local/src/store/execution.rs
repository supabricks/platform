use super::*;
use crate::{
    authorization::Grant,
    catalog::governance::{Broker, ReadCommand},
    execution::{self as run, Command, Dataset, Input, Manager, Prepared},
    identity::Context,
};
use serde_json::{Value, json};

fn admission(
    db: &Connection,
    ctx: &Context,
    deployment: &str,
    id: &str,
) -> Result<(String, String, String, i64)> {
    use super::authorization as a;
    a::require(db, ctx, deployment, 1)?;
    let (actor,effective,source,policy,state):(String,String,String,i64,String)=db.query_row("SELECT actor,effective_principal,source_revision,policy_revision,state FROM authorization_executions WHERE id=?1 AND deployment=?2",params![id,deployment],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?.ok_or_else(run::denied)?;
    if actor!=ctx.actor_id || state!="admitted" || a::policy(db,deployment)?!=policy || !a::granted(db,&actor,deployment,Grant::Execute,"","")? || !db.prepare("SELECT 1 FROM identity_principals WHERE id=?1 AND disabled=0 AND kind!='local_owner'")?.exists([&effective])? {return Err(run::denied());}
    if actor != effective
        && (!a::granted(db, &actor, deployment, Grant::ActAs, &effective, &source)?
            || a::role(db, &effective, deployment)? == 0
            || !a::granted(db, &effective, deployment, Grant::Execute, "", "")?)
    {
        return Err(run::denied());
    }
    Ok((actor, effective, source, policy))
}
fn session(db: &Connection, ctx: &Context, hash: &str) -> Result<()> {
    super::security::session(db, ctx, hash)
}
fn catalog(db: &Connection, revision: i64) -> Result<()> {
    if !db
        .prepare("SELECT 1 FROM catalog_governance WHERE state='ready' AND revision=?1")?
        .exists([revision])?
    {
        return Err(run::denied());
    }
    Ok(())
}
impl Store {
    pub(crate) fn recover_executions(&mut self) -> Result<()> {
        let work = self.root().join("isolated-work");
        if work.exists() {
            crate::catalog::config::directory(&work)?;
            for entry in std::fs::read_dir(work)? {
                let entry = entry?;
                if entry
                    .file_name()
                    .to_str()
                    .is_some_and(|v| v.starts_with("lease-"))
                    && entry.file_type()?.is_dir()
                {
                    std::fs::remove_dir_all(entry.path())?;
                }
            }
        }
        let tx = self.db.transaction()?;
        let records = tx.prepare("SELECT e.id,e.context_json,a.deployment,a.policy_revision,a.effective_principal FROM isolated_executions e JOIN authorization_executions a ON a.id=e.id WHERE e.state IN ('preparing','running')")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,i64>(3)?,r.get::<_,String>(4)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;
        tx.execute("UPDATE isolated_executions SET state='failed',result_json=NULL WHERE state IN ('preparing','running')",[])?;
        let room: i64 = tx.query_row("SELECT 10000-count(*) FROM security_audit", [], |r| {
            r.get(0)
        })?;
        if records.len() as i64 <= room {
            for (id, context, deployment, revision, effective) in records {
                super::authorization::audit(
                    &tx,
                    &serde_json::from_str(&context)?,
                    &deployment,
                    revision,
                    "runtime.recovered_closed",
                    &id,
                    &id,
                    &effective,
                )?;
            }
        } else {
            tx.execute(
                "UPDATE security_state SET recovery_pending=recovery_pending+?1",
                [records.len() as i64],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub(crate) fn execution_live(&self, id: &str) -> Result<()> {
        let (deployment,context,hash,revision):(String,String,String,i64)=self.db.query_row("SELECT a.deployment,e.context_json,e.token_hash,e.catalog_revision FROM isolated_executions e JOIN authorization_executions a ON a.id=e.id WHERE e.id=?1 AND e.state!='failed'",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
        let ctx: Context = serde_json::from_str(&context)?;
        admission(&self.db, &ctx, &deployment, id)?;
        session(&self.db, &ctx, &hash)?;
        catalog(&self.db, revision)
    }
    pub(crate) fn execution_finish(&mut self, id: &str, result: Option<Value>) -> Result<()> {
        let tx = self.db.transaction()?;
        let (context,deployment,policy,effective):(String,String,i64,String)=tx.query_row("SELECT e.context_json,a.deployment,a.policy_revision,a.effective_principal FROM isolated_executions e JOIN authorization_executions a ON a.id=e.id WHERE e.id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
        let ctx: Context = serde_json::from_str(&context)?;
        let state = if result.is_some() {
            "finished"
        } else {
            "failed"
        };
        tx.execute(
            "UPDATE isolated_executions SET state=?2,result_json=?3 WHERE id=?1",
            params![id, state, result.map(|v| v.to_string())],
        )?;
        super::authorization::audit(
            &tx,
            &ctx,
            &deployment,
            policy,
            "runtime.finish",
            id,
            id,
            &effective,
        )?;
        tx.commit()?;
        Ok(())
    }
    pub(crate) fn execution_job(
        &mut self,
        ctx: Context,
        hash: String,
        deployment: String,
        command: Command,
        broker: Broker,
        manager: Manager,
    ) -> Result<super::identity::IdentityJob> {
        let id = command.id().to_owned();
        run::uuid(&id)?;
        let (_, effective, source, policy) = admission(&self.db, &ctx, &deployment, &id)?;
        session(&self.db, &ctx, &hash)?;
        let snapshot = super::catalog_governance::snapshot(&self.db, &broker, &[])?;
        catalog(&self.db, snapshot.revision)?;
        let existing: Option<(String, String, String)> = self
            .db
            .query_row(
                "SELECT token_hash,datasets_json,state FROM isolated_executions WHERE id=?1",
                [&id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        let datasets: Vec<Dataset> = match &command {
            Command::Start { datasets, .. } => datasets.clone(),
            Command::Poll { .. } => {
                serde_json::from_str(&existing.as_ref().ok_or_else(run::denied)?.1)?
            }
        };
        if datasets.len() > 8 {
            return Err(run::denied());
        }
        let mut unique = std::collections::BTreeSet::new();
        for d in &datasets {
            run::uuid(&d.table)?;
            run::uuid(&d.publication)?;
            if !unique.insert(&d.table) {
                return Err(run::denied());
            }
        }
        let creating = existing.is_none();
        if let Some((saved, saved_data, state)) = &existing {
            if saved != &hash
                || serde_json::from_str::<Vec<Dataset>>(saved_data)? != datasets
                || state == "preparing"
            {
                return Err(run::denied());
            }
            self.execution_live(&id)?;
        }
        let mut inputs = Vec::new();
        if creating {
            let (total,active):(i64,i64)=self.db.query_row("SELECT count(*),coalesce(sum(state IN ('preparing','running')),0) FROM isolated_executions",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
            if total >= 128 || active >= 2 {
                return Err(run::denied());
            }
            for d in &datasets {
                let t = snapshot
                    .tables
                    .iter()
                    .find(|t| {
                        t.publication == d.publication
                            && t.revision == d.publication_revision
                            && t.object.id == d.table
                    })
                    .ok_or_else(run::denied)?;
                let p = self.catalog_publication(
                    t.deployment.parse().map_err(|_| run::denied())?,
                    d.publication.parse().map_err(|_| run::denied())?,
                )?;
                let table = p
                    .tables
                    .iter()
                    .find(|t| t.id.to_string() == d.table)
                    .ok_or_else(run::denied)?;
                let s = self.snapshot(p.project_id, p.epoch_id)?;
                let descriptor = s.publication.descriptor.ok_or_else(run::denied)?;
                if p.state != "published"
                    || s.state != "available"
                    || descriptor["manifest_sha256"] != p.manifest_hash
                {
                    return Err(run::denied());
                }
                let (root, descriptor) = crate::catalog::publication::read_view(
                    self,
                    s.publication.export_id,
                    &descriptor,
                )?;
                let m = descriptor["manifest"]["tables"]
                    .as_array()
                    .ok_or_else(run::denied)?
                    .iter()
                    .find(|v| v["schema"] == table.source_schema && v["name"] == table.source_name)
                    .ok_or_else(run::denied)?;
                let oid = m["oid"].as_u64().ok_or_else(run::denied)?;
                if m["path"] != oid.to_string() || m["version"] != 0 {
                    return Err(run::denied());
                }
                let prefix = format!("{oid}/");
                let files = descriptor["manifest"]["files"]
                    .as_array()
                    .ok_or_else(run::denied)?
                    .iter()
                    .filter(|v| v["path"].as_str().is_some_and(|p| p.starts_with(&prefix)))
                    .cloned()
                    .collect();
                inputs.push(Input {
                    root,
                    table: d.table.clone(),
                    files,
                });
            }
            let tx = self.db.transaction()?;
            super::authorization::audit(
                &tx,
                &ctx,
                &deployment,
                policy,
                "runtime.prepare",
                &id,
                &id,
                &effective,
            )?;
            tx.execute(
                "INSERT INTO isolated_executions VALUES (?1,?2,?3,?4,?5,'preparing',NULL,NULL)",
                params![
                    id,
                    hash,
                    serde_json::to_string(&ctx)?,
                    serde_json::to_string(&datasets)?,
                    snapshot.revision
                ],
            )?;
            tx.commit()?;
        }
        let (kind, contents): (String, String) = self.db.query_row(
            "SELECT kind,contents FROM authorization_sources WHERE deployment=?1 AND revision=?2",
            params![deployment, source],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let root = self.root().to_owned();
        // Slow inventory/copy work or a delayed writer cannot turn old
        // authentication into a newly renewed execution lease.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(25);
        Ok(Box::new(move || {
            let result = (|| {
                // UC supplies the effective-principal decision on every start,
                // renewal and result read. The user never gets a UC credential.
                if datasets.is_empty() {
                    broker.read(
                        &snapshot,
                        &effective,
                        ctx.expires_ms,
                        &ReadCommand::List {
                            search: String::new(),
                        },
                    )?;
                }
                for d in &datasets {
                    let value = broker.read(
                        &snapshot,
                        &effective,
                        ctx.expires_ms,
                        &ReadCommand::Describe {
                            publication: d.publication.clone(),
                            table: d.table.clone(),
                            publication_revision: d.publication_revision,
                        },
                    )?;
                    if value["item"].is_null() {
                        return Err(run::denied());
                    }
                }
                if creating {
                    let config = run::runtime::Config::load(&root)?;
                    let prepared = Prepared::build(&root, &kind, &contents, inputs)?;
                    // Copying and component verification cannot carry a stale UC
                    // decision forward. Recheck with a fresh bounded broker.
                    let broker = broker.fresh();
                    if datasets.is_empty() {
                        broker.read(
                            &snapshot,
                            &effective,
                            ctx.expires_ms,
                            &ReadCommand::List {
                                search: String::new(),
                            },
                        )?;
                    }
                    for d in &datasets {
                        if broker.read(
                            &snapshot,
                            &effective,
                            ctx.expires_ms,
                            &ReadCommand::Describe {
                                publication: d.publication.clone(),
                                table: d.table.clone(),
                                publication_revision: d.publication_revision,
                            },
                        )?["item"]
                            .is_null()
                        {
                            return Err(run::denied());
                        }
                    }
                    Ok(Some((prepared, config)))
                } else {
                    Ok(None)
                }
            })();
            Ok(Box::new(move |store| {
                let finish = (|| {
                    if std::time::Instant::now() >= deadline {
                        return Err(run::denied());
                    }
                    store.execution_live(&id)?;
                    session(&store.db, &ctx, &hash)?;
                    let prepared = result?;
                    if let Some((prepared, config)) = prepared {
                        let tx = store.db.transaction()?;
                        super::authorization::audit(
                            &tx,
                            &ctx,
                            &deployment,
                            policy,
                            "runtime.launch",
                            &id,
                            &id,
                            &effective,
                        )?;
                        tx.execute(
                            "UPDATE isolated_executions SET state='running',runtime_identity=?2 WHERE id=?1",
                            params![id,crate::project_apply::digest(&config)?],
                        )?;
                        tx.commit()?;
                        manager.start(&id, prepared, config)?;
                    } else {
                        let state: String = store.db.query_row(
                            "SELECT state FROM isolated_executions WHERE id=?1",
                            [&id],
                            |r| r.get(0),
                        )?;
                        if state == "running" {
                            super::authorization::audit(
                                &store.db,
                                &ctx,
                                &deployment,
                                policy,
                                "runtime.renew",
                                &id,
                                &id,
                                &effective,
                            )?;
                            manager.renew(&id)?;
                        }
                    }
                    let (state, result): (String, Option<String>) = store.db.query_row(
                        "SELECT state,result_json FROM isolated_executions WHERE id=?1",
                        [&id],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )?;
                    Ok(
                        json!({"id":id,"state":state,"lease_ms":run::LEASE_MS,"datasets":datasets,"result":result.map(|v|serde_json::from_str::<Value>(&v)).transpose()?}),
                    )
                })();
                if finish.is_err() {
                    let _ = store.execution_finish(&id, None);
                }
                finish
            }))
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        authorization::{AdminCommand, Command as A, Role, Subject},
        identity::{self, Channel},
    };
    struct Fixture {
        _dir: tempfile::TempDir,
        store: Store,
        ctx: Context,
        hash: String,
        deployment: String,
        id: String,
        broker: Broker,
    }
    fn fixture() -> Fixture {
        let (dir, mut store, owner, p) = crate::catalog::publication::tests::setup();
        let deployment = owner.deployment_id.to_string();
        let actor = identity::id();
        store
            .db
            .execute(
                "INSERT INTO identity_principals(id,kind,label) VALUES (?1,'service','runner')",
                [&actor],
            )
            .unwrap();
        let token = store
            .identity_admin(identity::AdminCommand::IssueService {
                principal: actor.clone(),
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
                .identity_auth(identity::AuthCommand::Authenticate {
                    token: token.clone(),
                    channel: Channel::Service,
                    csrf: None,
                })
                .unwrap(),
        )
        .unwrap();
        for execute in [false, true] {
            let revision = super::super::authorization::policy(&store.db, &deployment).unwrap();
            store
                .authorization_admin(if execute {
                    AdminCommand::SetGrant {
                        deployment: deployment.clone(),
                        subject: Subject::Principal(actor.clone()),
                        grant: Grant::Execute,
                        effective_principal: None,
                        source_revision: None,
                        present: true,
                        expected_policy: revision,
                        key: identity::id(),
                    }
                } else {
                    AdminCommand::SetRole {
                        deployment: deployment.clone(),
                        subject: Subject::Principal(actor.clone()),
                        role: Some(Role::Editor),
                        expected_policy: revision,
                        key: identity::id(),
                    }
                })
                .unwrap();
        }
        let revision = super::super::authorization::policy(&store.db, &deployment).unwrap();
        let source = store
            .authorized_command(
                &ctx,
                A::SaveSource {
                    deployment: deployment.clone(),
                    asset: "query".into(),
                    kind: "sql".into(),
                    contents: "select 42".into(),
                    expected_head: None,
                    expected_policy: revision,
                    key: identity::id(),
                },
            )
            .unwrap()["revision"]
            .as_str()
            .unwrap()
            .to_owned();
        let id = store
            .authorized_command(
                &ctx,
                A::AdmitExecution {
                    deployment: deployment.clone(),
                    source_revision: source,
                    effective_principal: None,
                    expected_policy: revision,
                    key: identity::id(),
                },
            )
            .unwrap()["id"]
            .as_str()
            .unwrap()
            .to_owned();
        store
            .db
            .execute("UPDATE catalog_governance SET state='ready'", [])
            .unwrap();
        let broker = Broker::snapshot_fixture(p.namespace.provider_id, p.namespace.metastore_id);
        Fixture {
            _dir: dir,
            store,
            ctx,
            hash: identity::hash(&token),
            deployment,
            id,
            broker,
        }
    }
    fn job(f: &mut Fixture) -> Result<super::super::identity::IdentityJob> {
        f.store.execution_job(
            f.ctx.clone(),
            f.hash.clone(),
            f.deployment.clone(),
            Command::Start {
                id: f.id.clone(),
                datasets: vec![],
            },
            f.broker.fresh(),
            Manager::default(),
        )
    }
    #[test]
    fn isolated_admission_binds_actor_source_policy_session_and_audit_before_effects() {
        let mut f = fixture();
        assert!(admission(&f.store.db, &f.ctx, &f.deployment, &f.id).is_ok());
        let mut other = f.ctx.clone();
        other.actor_id = identity::id();
        other.effective_principal_id = other.actor_id.clone();
        assert!(admission(&f.store.db, &other, &f.deployment, &f.id).is_err());
        f.store.db.execute_batch("CREATE TRIGGER fail_runtime_audit BEFORE INSERT ON authorization_audit BEGIN SELECT RAISE(ABORT,'disk full'); END;").unwrap();
        assert!(job(&mut f).is_err());
        assert_eq!(
            f.store
                .db
                .query_row("SELECT count(*) FROM isolated_executions", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        f.store
            .db
            .execute_batch("DROP TRIGGER fail_runtime_audit")
            .unwrap();
        let pending = job(&mut f).unwrap();
        assert!(job(&mut f).is_err());
        // A provider outage returns an error and persists a terminal denial.
        assert!(pending().unwrap()(&mut f.store).is_err());
        assert!(f.store.execution_live(&f.id).is_err());
        assert_eq!(
            f.store
                .db
                .query_row("SELECT state FROM isolated_executions", [], |r| r
                    .get::<_, String>(0))
                .unwrap(),
            "failed"
        );
        let mut f = fixture();
        f.store
            .db
            .execute("UPDATE authorization_policy SET revision=revision+1", [])
            .unwrap();
        assert!(job(&mut f).is_err());
        let mut f = fixture();
        f.store
            .db
            .execute("DELETE FROM identity_sessions", [])
            .unwrap();
        assert!(job(&mut f).is_err());
        let mut f = fixture();
        f.store
            .db
            .execute(
                "UPDATE identity_principals SET disabled=1 WHERE id=?1",
                [&f.ctx.actor_id],
            )
            .unwrap();
        assert!(job(&mut f).is_err());
        let mut f = fixture();
        let _pending = job(&mut f).unwrap();
        f.store.recover_executions().unwrap();
        assert!(f.store.execution_live(&f.id).is_err());
    }
}
