use super::{authorization as a, *};
use crate::{
    authorization,
    console::governed::Command,
    identity::{self, Context},
};
use serde_json::{Value, json};
use std::{fs, net::TcpListener, os::unix::fs::DirBuilderExt};

pub(super) fn realm_admin(db: &Connection, ctx: &Context) -> Result<()> {
    a::validate_context(db, ctx)?;
    if !db
        .prepare("SELECT 1 FROM identity_realm WHERE bootstrap_principal=?1")?
        .exists([&ctx.actor_id])?
    {
        return Err(authorization::denied());
    }
    Ok(())
}
// Public identifiers and review hashes are sufficient for the browser. Native
// storage locations and diagnostic errors stay on the operator boundary.
fn redact_publication(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for key in ["storage_location", "descriptor", "error"] {
                map.remove(key);
            }
            for v in map.values_mut() {
                redact_publication(v);
            }
        }
        Value::Array(items) => {
            for v in items {
                redact_publication(v);
            }
        }
        _ => {}
    }
}
impl Store {
    pub(crate) fn workspace_command(&mut self, ctx: &Context, command: Command) -> Result<Value> {
        a::validate_context(&self.db, ctx)?;
        match command {
            Command::Sync {
                deployment,
                request,
            } => self.governed_sync(ctx, &deployment, request),
            Command::Snapshot { deployment, export } => {
                a::require(&self.db, ctx, &deployment, 1)?;
                let id = export.parse().map_err(|_| authorization::denied())?;
                if !self.governed_sync_export(ctx, &deployment, id)? && !self.db.prepare("SELECT 1 FROM data_operations WHERE actor=?1 AND deployment=?2 AND result_json->>'$.export_id'=?3")?.exists(params![ctx.actor_id,deployment,export])? {return Err(authorization::denied());}
                self.data_export_live(id, false)?;
                let e = self.export(id)?;
                let publication = self
                    .publication(id)
                    .ok()
                    .map(|p| json!({"epoch_id":p.epoch_id,"state":p.state}));
                Ok(json!({"export_id":export,"state":e.state,"publication":publication}))
            }
            Command::Context {} => {
                let label: String = self.db.query_row(
                    "SELECT label FROM identity_principals WHERE id=?1",
                    [&ctx.actor_id],
                    |r| r.get(0),
                )?;
                Ok(
                    json!({"identity":ctx,"label":label,"realm_administrator":realm_admin(&self.db,ctx).is_ok(),"authorization":"governed"}),
                )
            }
            Command::Branches { deployment } => {
                a::require(&self.db, ctx, &deployment, 1)?;
                let branches = self.db.prepare("SELECT b.id,b.name,CASE WHEN b.revision=b.observed_revision THEN b.desired ELSE 'starting' END,b.revision,b.observed_revision FROM branches b JOIN deployments d ON d.runtime_project_id=b.project_id WHERE d.id=?1 AND b.expired=0 AND b.desired!='deleted' ORDER BY b.name")?.query_map([&deployment],|r| Ok(json!({"id":r.get::<_,String>(0)?,"name":r.get::<_,String>(1)?,"state":r.get::<_,String>(2)?,"revision":r.get::<_,i64>(3)?,"observed_revision":r.get::<_,i64>(4)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
                Ok(
                    json!({"branches":branches,"capabilities":{"sync_controls":1,"managed_snapshot_scheduling":true,"incremental_triggered":false,"continuous_sync":false,"sync_event_triggers":false}}),
                )
            }
            Command::CreateProject { name, key } => {
                realm_admin(&self.db, ctx)?;
                self.governed_create(ctx, &name, &key)
            }
            Command::Policy { command } => {
                realm_admin(&self.db, ctx)?;
                self.authorization_admin_as(ctx, command)
            }
            Command::Audit { after } => {
                realm_admin(&self.db, ctx)?;
                super::security::export(&self.db, after)
            }
            Command::Catalog { .. } | Command::Publication { .. } | Command::Namespace { .. } => {
                Err(authorization::denied())
            }
            command => {
                realm_admin(&self.db, ctx)?;
                let command = match command {
                    Command::Directory {} => identity::AdminCommand::Status,
                    Command::Group { label } => identity::AdminCommand::Group { label },
                    Command::Service { label } => identity::AdminCommand::Service { label },
                    Command::Membership {
                        group,
                        principal,
                        present,
                    } => identity::AdminCommand::Membership {
                        group,
                        principal,
                        present,
                    },
                    Command::Disable {
                        principal,
                        disabled,
                    } => identity::AdminCommand::Disable {
                        principal,
                        disabled,
                    },
                    Command::Revoke { principal } => identity::AdminCommand::Revoke { principal },
                    _ => return Err(authorization::denied()),
                };
                self.identity_admin_as(&ctx.actor_id, command)
            }
        }
    }
    fn governed_create(&mut self, ctx: &Context, name: &str, key: &str) -> Result<Value> {
        authorization::key(key)?;
        let name = canonical_name(name)?;
        let directory = identity::hash(&format!("{}:{key}", ctx.actor_id));
        self.db.execute("INSERT OR IGNORE INTO governed_project_requests(actor,request_key,name,directory) VALUES (?1,?2,?3,?4)",params![ctx.actor_id,key,name,directory])?;
        let (saved,directory,deployment,branch):(String,String,Option<String>,Option<String>) = self.db.query_row("SELECT name,directory,deployment,branch FROM governed_project_requests WHERE actor=?1 AND request_key=?2",params![ctx.actor_id,key],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
        if saved != name {
            return Err(conflict(
                "project request key was already used for another name",
            ));
        }
        if let (Some(deployment), Some(branch)) = (&deployment, &branch) {
            return Ok(json!({"project":a::project(&self.db,deployment)?,"branch":branch}));
        }
        let base = self.root().join("governed-projects");
        for path in [&base, &base.join(directory)] {
            match fs::DirBuilder::new().mode(0o700).create(path) {
                Ok(()) => fs::File::open(path.parent().unwrap())?.sync_all()?,
                Err(e)
                    if e.kind() == std::io::ErrorKind::AlreadyExists
                        && fs::symlink_metadata(path)?.is_dir() => {}
                Err(e) => return Err(e.into()),
            }
        }
        let path = base.join(identity::hash(&format!("{}:{key}", ctx.actor_id)));
        crate::project::ProjectConfig::initialize(&path, &name)?;
        let source = crate::deployments::Source::read(&path)?;
        self.db.execute_batch("SAVEPOINT governed_create")?;
        let initialized = (|| -> Result<crate::deployments::Context> {
            let context = self.resolve_deployment(&source)?;
            if deployment.is_none() {
                let id = context.deployment_id.to_string();
                self.db.execute(
                    "INSERT INTO authorization_roles VALUES (?1,?2,'administrator')",
                    params![id, format!("principal:{}", ctx.actor_id)],
                )?;
                self.db.execute(
                    "UPDATE authorization_policy SET revision=revision+1 WHERE deployment=?1",
                    [&id],
                )?;
                self.db.execute("UPDATE governed_project_requests SET deployment=?1 WHERE actor=?2 AND request_key=?3",params![id,ctx.actor_id,key])?;
                a::audit(
                    &self.db,
                    ctx,
                    &id,
                    a::policy(&self.db, &id)?,
                    "project.create",
                    key,
                    &id,
                    &ctx.actor_id,
                )?;
            }
            Ok(context)
        })();
        let context = match initialized {
            Ok(v) => {
                self.db.execute_batch("RELEASE governed_create")?;
                v
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO governed_create; RELEASE governed_create")?;
                return Err(e);
            }
        };
        // Existing native intent determines retry ports and branch identity.
        let native_key = format!("governed-console:{}", identity::hash(key));
        let reservations = (0..3)
            .map(|_| TcpListener::bind("127.0.0.1:0"))
            .collect::<std::io::Result<Vec<_>>>()?;
        let ports = crate::operations::Ports {
            sql: reservations[0].local_addr()?.port(),
            external_http: reservations[1].local_addr()?.port(),
            internal_http: reservations[2].local_addr()?.port(),
        };
        let mutation = self
            .request_for_key(context.runtime_project_id, &native_key)?
            .unwrap_or(crate::operations::Mutation::CreateDatabase {
                name: "main".into(),
                ports,
            });
        let operation = self.submit(context.runtime_project_id, &native_key, mutation)?;
        let branch = operation.branch_id.to_string();
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT OR IGNORE INTO governed_branches VALUES (?1,'ready')",
            [&branch],
        )?;
        tx.execute(
            "UPDATE governed_project_requests SET branch=?1 WHERE actor=?2 AND request_key=?3",
            params![branch, ctx.actor_id, key],
        )?;
        tx.commit()?;
        Ok(
            json!({"project":a::project(&self.db,&context.deployment_id.to_string())?,"branch":branch}),
        )
    }
    pub(crate) fn workspace_job(
        &mut self,
        ctx: Context,
        token_hash: String,
        command: Command,
        manager: &crate::catalog::Manager,
        metadata: &mut crate::catalog::metadata::Service,
    ) -> Result<super::identity::IdentityJob> {
        super::security::session(&self.db, &ctx, &token_hash)?;
        match command {
            Command::Catalog { command } => {
                realm_admin(&self.db, &ctx)?;
                let broker = crate::catalog::governance::Broker::managed(manager, self)?;
                let job =
                    self.catalog_governance_admin_as(ctx.actor_id.clone(), command, broker)?;
                Ok(Box::new(move || {
                    let commit = job()?;
                    Ok(Box::new(move |store| {
                        super::security::session(&store.db, &ctx, &token_hash)?;
                        realm_admin(&store.db, &ctx)?;
                        commit(store)
                    }))
                }))
            }
            Command::Namespace { deployment, ensure } => {
                a::require(&self.db, &ctx, &deployment, 3)?;
                let context =
                    self.deployment(deployment.parse().map_err(|_| authorization::denied())?)?;
                let path:String = self.db.query_row("SELECT path FROM worktree_bindings WHERE deployment_id=?1 ORDER BY path LIMIT 1",[&deployment],|r|r.get(0))?;
                if !ensure {
                    let provider = manager.adapter(self)?;
                    let value = json!({"namespace":self.catalog_namespace(context.deployment_id,&provider.provider_id)?});
                    return Ok(Box::new(move || {
                        Ok(Box::new(move |store| {
                            super::security::session(&store.db, &ctx, &token_hash)?;
                            a::require(&store.db, &ctx, &deployment, 3)?;
                            Ok(value)
                        }))
                    }));
                }
                let value = metadata.handle(
                    self,
                    manager,
                    &context.binding(Path::new(&path)),
                    crate::catalog::metadata::Command::EnsureNamespace {},
                    None,
                )?;
                Ok(Box::new(move || {
                    Ok(Box::new(move |store| {
                        super::security::session(&store.db, &ctx, &token_hash)?;
                        a::require(&store.db, &ctx, &deployment, 3)?;
                        Ok(value)
                    }))
                }))
            }
            Command::Publication {
                deployment,
                command,
            } => {
                // Publishing may expose branch snapshots: require explicit Share,
                // checked against the original governed export below.
                a::require(&self.db, &ctx, &deployment, 3)?;
                let context =
                    self.deployment(deployment.parse().map_err(|_| authorization::denied())?)?;
                let path:String = self.db.query_row("SELECT path FROM worktree_bindings WHERE deployment_id=?1 ORDER BY path LIMIT 1",[&deployment],|r|r.get(0))?;
                let epoch = match &command {
                    crate::catalog::publication::Command::Preview { epoch_id }
                    | crate::catalog::publication::Command::Publish { epoch_id, .. } => {
                        Some(*epoch_id)
                    }
                    _ => None,
                };
                if let Some(epoch) = epoch {
                    let snapshot = self.snapshot(context.runtime_project_id, epoch)?;
                    let export = snapshot.publication.export_id;
                    if !self.governed_sync_export(&ctx, &deployment, export)? && !self.db.prepare("SELECT 1 FROM data_operations WHERE actor=?1 AND deployment=?2 AND result_json->>'$.export_id'=?3")?.exists(params![ctx.actor_id,deployment,export.to_string()])? { return Err(authorization::denied()); }
                    self.data_export_live(export, true)?;
                }
                let branch = match &command {
                    crate::catalog::publication::Command::Preview { epoch_id }
                    | crate::catalog::publication::Command::Publish { epoch_id, .. } => {
                        self.snapshot(context.runtime_project_id, *epoch_id)?
                            .publication
                            .branch_id
                    }
                    crate::catalog::publication::Command::Resolve { branch } => {
                        crate::api::resolve(
                            self,
                            &context.binding(Path::new(&path)),
                            branch.as_deref(),
                        )?
                    }
                    crate::catalog::publication::Command::Status { id }
                    | crate::catalog::publication::Command::Resume { id }
                    | crate::catalog::publication::Command::Unpublish { id, .. } => {
                        self.catalog_publication(context.deployment_id, *id)?
                            .branch_id
                    }
                };
                super::governed::grant(
                    &self.db,
                    &ctx,
                    &deployment,
                    &branch.to_string(),
                    crate::governed::Capability::Share,
                )?;
                let request = serde_json::to_value(&command)?;
                a::audit(
                    &self.db,
                    &ctx,
                    &deployment,
                    a::policy(&self.db, &deployment)?,
                    &format!("publication.{}", request["action"].as_str().unwrap()),
                    request["key"].as_str().unwrap_or(""),
                    &branch.to_string(),
                    &ctx.actor_id,
                )?;
                let mut value = crate::catalog::publication::handle(
                    self,
                    manager,
                    &context.binding(Path::new(&path)),
                    command,
                )?;
                redact_publication(&mut value);
                Ok(Box::new(move || {
                    Ok(Box::new(move |store| {
                        super::security::session(&store.db, &ctx, &token_hash)?;
                        a::require(&store.db, &ctx, &deployment, 3)?;
                        Ok(value)
                    }))
                }))
            }
            _ => Err(authorization::denied()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn user(store: &mut Store, label: &str) -> Context {
        let value = store
            .identity_admin(identity::AdminCommand::Service {
                label: label.into(),
            })
            .unwrap();
        let actor = value["principal_id"].as_str().unwrap().to_owned();
        Context {
            api_version: 1,
            realm_id: store
                .db
                .query_row("SELECT id FROM identity_realm", [], |r| r.get(0))
                .unwrap(),
            actor_id: actor.clone(),
            effective_principal_id: actor,
            channel: identity::Channel::Browser,
            scopes: vec![authorization::CONTROL_SCOPE.into()],
            expires_ms: identity::now() + 60000,
        }
    }
    #[test]
    fn governed_console_creation_retries_without_restoring_revoked_roles_or_implicit_data() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("state")).unwrap();
        let alice = user(&mut store, "alice");
        let bob = user(&mut store, "bob");
        store
            .db
            .execute(
                "UPDATE identity_realm SET bootstrap_principal=?1",
                [&alice.actor_id],
            )
            .unwrap();
        let create = || Command::CreateProject {
            name: "governed".into(),
            key: "create-one".into(),
        };
        assert!(store.workspace_command(&bob, create()).is_err());
        assert!(
            store
                .workspace_command(&bob, Command::Directory {})
                .is_err()
        );
        let first = store.workspace_command(&alice, create()).unwrap();
        let deployment = first["project"]["deployment_id"].as_str().unwrap();
        assert_eq!(store.workspace_command(&alice, create()).unwrap(), first);
        assert!(
            store
                .workspace_command(
                    &alice,
                    Command::CreateProject {
                        name: "changed".into(),
                        key: "create-one".into()
                    }
                )
                .is_err()
        );
        assert_eq!(
            store
                .db
                .query_row("SELECT count(*) FROM data_grants", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            store
                .db
                .query_row("SELECT count(*) FROM authorization_grants", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert!(
            store
                .governed_branch(first["branch"].as_str().unwrap().parse().unwrap())
                .unwrap()
        );
        assert!(
            store
                .workspace_command(
                    &bob,
                    Command::Branches {
                        deployment: deployment.into()
                    }
                )
                .is_err()
        );
        let actor: String = store
            .db
            .query_row(
                "SELECT actor FROM authorization_audit WHERE action='project.create'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(actor, alice.actor_id);
        store
            .db
            .execute(
                "DELETE FROM authorization_roles WHERE deployment=?1",
                [deployment],
            )
            .unwrap();
        store.workspace_command(&alice, create()).unwrap();
        assert_eq!(a::role(&store.db, &alice.actor_id, deployment).unwrap(), 0);
        // Restart after a native intent committed but before enrollment is still governed.
        store
            .db
            .execute("DELETE FROM governed_branches", [])
            .unwrap();
        store
            .db
            .execute("UPDATE governed_project_requests SET branch=NULL", [])
            .unwrap();
        assert!(
            store
                .governed_branch(first["branch"].as_str().unwrap().parse().unwrap())
                .unwrap()
        );
        store.workspace_command(&alice, create()).unwrap();
        assert_eq!(a::role(&store.db, &alice.actor_id, deployment).unwrap(), 0);
    }
    #[test]
    fn delegated_identity_admin_is_audited_and_cannot_disable_local_owner() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(&dir.path().join("state")).unwrap();
        let alice = user(&mut store, "alice");
        let bob = user(&mut store, "bob");
        store
            .db
            .execute(
                "UPDATE identity_realm SET bootstrap_principal=?1",
                [&alice.actor_id],
            )
            .unwrap();
        let owner: String = store
            .db
            .query_row("SELECT local_owner FROM identity_realm", [], |r| r.get(0))
            .unwrap();
        assert!(
            store
                .workspace_command(
                    &alice,
                    Command::Disable {
                        principal: owner,
                        disabled: true
                    }
                )
                .is_err()
        );
        store
            .workspace_command(
                &alice,
                Command::Disable {
                    principal: bob.actor_id.clone(),
                    disabled: true,
                },
            )
            .unwrap();
        let actor: String = store
            .db
            .query_row(
                "SELECT actor FROM identity_audit WHERE action='principal.disable'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(actor, alice.actor_id);
        assert!(store.workspace_command(&bob, Command::Context {}).is_err());
        for wire in [
            json!({"action":"configure","provider":"evil","config":{}}),
            json!({"action":"audit_acknowledge","after":0,"through":1,"sha256":"x"}),
            json!({"action":"create_project","name":"ok","key":"k","path":"/tmp/evil"}),
        ] {
            assert!(serde_json::from_value::<Command>(wire).is_err());
        }
    }
}
