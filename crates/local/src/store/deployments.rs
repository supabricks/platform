use super::{
    Result, Store,
    error::{conflict, invalid, missing},
};
use crate::{
    api::Binding,
    deployments::{Command, Context, Source},
    project::ProjectConfig,
};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use std::{os::unix::fs::MetadataExt, path::Path};
use supabricks_core::resource::{DeploymentId, ProjectId};
impl Store {
    fn owner_context(&self) -> Result<(String, String, String)> {
        Ok(self.db.query_row("SELECT r.id,w.id,p.id FROM realms r JOIN workspaces w ON w.realm_id=r.id JOIN principals p ON p.realm_id=r.id WHERE r.name='local' AND w.name='local' AND p.provider='local-owner'", [], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?)
    }
    pub(crate) fn ensure_legacy_deployment(&self, config: &ProjectConfig) -> Result<()> {
        if self
            .db
            .prepare("SELECT 1 FROM deployments WHERE runtime_project_id=?1")?
            .exists([config.id.to_string()])?
        {
            return Ok(());
        }
        let (_, workspace, actor) = self.owner_context()?;
        self.db.execute(
            "INSERT INTO project_definitions VALUES (?1,?2) ON CONFLICT(id) DO NOTHING",
            params![config.id.to_string(), config.name],
        )?;
        self.db.execute(
            "INSERT INTO deployments VALUES (?1,?2,?3,?2,'local',1,1,?4,?4)",
            params![
                DeploymentId::new().to_string(),
                config.id.to_string(),
                workspace,
                actor
            ],
        )?;
        Ok(())
    }
    pub fn deployment(&self, id: DeploymentId) -> Result<Context> {
        let row=self.db.query_row("SELECT d.definition_id,d.runtime_project_id,d.workspace_id,w.realm_id,d.target,d.legacy,d.revision,d.actor_id,d.effective_principal_id FROM deployments d JOIN workspaces w ON w.id=d.workspace_id WHERE d.id=?1",[id.to_string()],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?))).optional()?.ok_or_else(||missing("deployment in local workspace"))?;
        Ok(Context {
            api_version: 1,
            definition_id: super::parse(&row.0)?,
            deployment_id: id,
            runtime_project_id: super::parse(&row.1)?,
            workspace_id: row.2,
            realm_id: row.3,
            target: row.4,
            legacy: row.5,
            revision: row.6,
            actor_id: row.7,
            effective_principal_id: row.8,
            identity_provider: "local-owner".into(),
        })
    }
    fn source_config(&self, source: &Source) -> Result<(ProjectConfig, u32)> {
        if !source.worktree.is_absolute() || source.worktree.canonicalize()? != source.worktree {
            return Err(invalid(
                "deployment worktree must be canonical and absolute",
            ));
        }
        let (config, version) = crate::projects::source_identity_version(&source.worktree)?;
        if config.id != source.definition_id {
            return Err(conflict("project definition changed; reopen the session"));
        }
        Ok((config, version))
    }
    pub fn resolve_deployment(&mut self, source: &Source) -> Result<Context> {
        let (config, format) = self.source_config(source)?;
        if self
            .db
            .prepare("SELECT 1 FROM worktree_bindings WHERE path=?1")?
            .exists([super::canonical_path(&source.worktree)?])?
        {
            return self.bound_context(source, format, true);
        }
        // Preserve init + first-use convenience for a genuinely new format-1 project.
        // Existing UUIDs, including legacy roots without worktree evidence, require attach.
        if format==1 && !self.db.prepare("SELECT 1 FROM projects WHERE id=?1 UNION SELECT 1 FROM project_definitions WHERE id=?1")?.exists([config.id.to_string()])? {
            self.db.execute_batch("SAVEPOINT initial_binding")?;
            let result=(|| {
                self.register_project(&config)?;
                let id:String=self.db.query_row("SELECT id FROM deployments WHERE runtime_project_id=?1",[config.id.to_string()],|r|r.get(0))?;
                let context=self.deployment(super::parse(&id)?)?;
                self.attach(source,format,&context)?;
                Ok(context)
            })();
            return self.finish_binding(result);
        }
        Err(conflict(
            "project is unbound: use project create for a new deployment, project attach for an existing deployment, or project adopt for a legacy project",
        ))
    }
    fn finish_binding<T>(&self, result: Result<T>) -> Result<T> {
        match result {
            Ok(v) => {
                self.db.execute_batch("RELEASE initial_binding")?;
                Ok(v)
            }
            Err(e) => {
                self.db
                    .execute_batch("ROLLBACK TO initial_binding; RELEASE initial_binding")?;
                Err(e)
            }
        }
    }
    fn bound_context(&self, source: &Source, format: u32, pin: bool) -> Result<Context> {
        let row:Option<(String,u32,Option<String>,Option<String>)>=self.db.query_row("SELECT deployment_id,source_format,device,inode FROM worktree_bindings WHERE path=?1",[super::canonical_path(&source.worktree)?],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
        let (id, expected, device, inode) = row
            .ok_or_else(|| conflict("worktree is unbound; explicitly attach it to a deployment"))?;
        let context = self.deployment(super::parse(&id)?)?;
        if context.definition_id != source.definition_id || expected != format {
            return Err(conflict(
                "bound definition or format changed; use explicit adoption, never implicit rebinding",
            ));
        }
        let m = std::fs::metadata(&source.worktree)?;
        if device.as_ref().is_some_and(|d| *d != m.dev().to_string())
            || inode.as_ref().is_some_and(|i| *i != m.ino().to_string())
        {
            return Err(conflict(
                "bound worktree directory was replaced; use explicit attach to confirm this directory",
            ));
        }
        if pin && device.is_none() {
            self.db.execute(
                "UPDATE worktree_bindings SET device=?1,inode=?2 WHERE path=?3 AND device IS NULL",
                params![
                    m.dev().to_string(),
                    m.ino().to_string(),
                    super::canonical_path(&source.worktree)?
                ],
            )?;
        }
        Ok(context)
    }
    pub fn check_runtime_binding(&self, path: &Path, runtime: ProjectId) -> Result<()> {
        let source = Source::read(path)?;
        let (_, format) = self.source_config(&source)?;
        if self
            .bound_context(&source, format, true)?
            .runtime_project_id
            != runtime
        {
            return Err(conflict(
                "runtime project differs from the worktree deployment",
            ));
        }
        Ok(())
    }
    pub fn binding_context(&self, binding: &Binding) -> Result<Context> {
        let source = Source::read(&binding.worktree)?;
        let (_, format) = self.source_config(&source)?;
        let context = self.bound_context(&source, format, true)?;
        if context.runtime_project_id != binding.project_id {
            return Err(conflict("runtime identity differs from deployment"));
        }
        Ok(context)
    }
    pub fn admit_binding(&mut self, binding: &Binding) -> Result<()> {
        let source = Source::read(&binding.worktree)?;
        if source.worktree != binding.worktree {
            return Err(invalid("binding worktree must be canonical"));
        }
        let context = self.resolve_deployment(&source)?;
        if context.runtime_project_id != binding.project_id {
            return Err(conflict(
                "runtime project differs from the resolved deployment; reopen the session",
            ));
        }
        Ok(())
    }
    fn attach(&self, source: &Source, format: u32, context: &Context) -> Result<()> {
        if context.definition_id != source.definition_id {
            return Err(conflict("deployment belongs to another source definition"));
        }
        let path = super::canonical_path(&source.worktree)?;
        let old: Option<String> = self
            .db
            .query_row(
                "SELECT deployment_id FROM worktree_bindings WHERE path=?1",
                [&path],
                |r| r.get(0),
            )
            .optional()?;
        if old.is_some_and(|id| id != context.deployment_id.to_string()) {
            return Err(conflict(
                "worktree already attached to another deployment; use a separate checkout",
            ));
        }
        let m = std::fs::metadata(&source.worktree)?;
        self.db.execute("INSERT INTO worktree_bindings VALUES (?1,?2,?3,?4,?5,?6,?6) ON CONFLICT(path) DO UPDATE SET source_format=excluded.source_format,device=excluded.device,inode=excluded.inode",params![path,context.deployment_id.to_string(),format,m.dev().to_string(),m.ino().to_string(),context.actor_id])?;
        Ok(())
    }
    pub fn project_command(&mut self, source: &Source, command: Command) -> Result<Value> {
        let (config, format) = self.source_config(source)?;
        if matches!(command, Command::Inspect) {
            return Ok(serde_json::to_value(
                self.bound_context(source, format, true)?,
            )?);
        }
        if matches!(command, Command::List) {
            let ids = self
                .db
                .prepare("SELECT id FROM deployments WHERE definition_id=?1 ORDER BY id")?
                .query_map([config.id.to_string()], |r| r.get::<_, String>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            let deployments = ids
                .iter()
                .map(|id| self.deployment(super::parse(id)?))
                .collect::<Result<Vec<_>>>()?;
            return Ok(json!({"api_version":1,"deployments":deployments}));
        }
        if let Command::Attach { deployment } = command {
            let context = self.deployment(deployment)?;
            if format == 2 && context.legacy {
                return Err(conflict(
                    "legacy deployment requires project adopt before attaching a format-2 definition",
                ));
            }
            if format == 2 {
                crate::projects::inspect(&source.worktree, Some(&context.target))?;
            }
            self.attach(source, format, &context)?;
            return Ok(serde_json::to_value(context)?);
        }
        let (key, target) = match &command {
            Command::Create { key, target } | Command::Adopt { key, target, .. } => (key, target),
            _ => unreachable!(),
        };
        if key.is_empty() || key.len() > 256 {
            return Err(invalid("deployment request key requires 1–256 bytes"));
        }
        let inspection = crate::projects::inspect(&source.worktree, target.as_deref())?;
        if inspection.definition.id != source.definition_id {
            return Err(conflict(
                "project definition changed during deployment inspection; retry",
            ));
        }
        if format != 2 {
            return Err(invalid(
                "create/adopt require an explicit format-2 definition; legacy projects use attach",
            ));
        }
        let request=json!({"command":command,"worktree":source.worktree,"source_sha256":inspection.source_sha256}).to_string();
        if let Some((old,id))=self.db.query_row("SELECT request,deployment_id FROM binding_operations WHERE definition_id=?1 AND request_key=?2",params![config.id.to_string(),key],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional()? {
            if old!=request {return Err(conflict("deployment request key already used with different inputs"));}
            return Ok(serde_json::to_value(self.bound_context(source,format,true).and_then(|c|if c.deployment_id.to_string()==id{Ok(c)}else{Err(conflict("deployment retry binding differs"))})?)?);
        }
        self.db.execute_batch("SAVEPOINT initial_binding")?;
        let result = (|| {
            let (_, workspace, actor) = self.owner_context()?;
            self.db.execute("INSERT INTO project_definitions VALUES (?1,?2) ON CONFLICT(id) DO UPDATE SET name=excluded.name",params![config.id.to_string(),config.name])?;
            let id = match &command {
                Command::Create { .. } => {
                    if self
                        .db
                        .prepare("SELECT 1 FROM worktree_bindings WHERE path=?1")?
                        .exists([super::canonical_path(&source.worktree)?])?
                    {
                        return Err(conflict(
                            "worktree already has a deployment; create from a separate checkout",
                        ));
                    }
                    let runtime = ProjectId::new();
                    let id = DeploymentId::new();
                    self.db.execute(
                        "INSERT INTO projects VALUES (?1,?2)",
                        params![runtime.to_string(), config.name],
                    )?;
                    self.db.execute(
                        "INSERT INTO deployments VALUES (?1,?2,?3,?4,?5,0,1,?6,?6)",
                        params![
                            id.to_string(),
                            config.id.to_string(),
                            workspace,
                            runtime.to_string(),
                            inspection.target,
                            actor
                        ],
                    )?;
                    id
                }
                Command::Adopt {
                    runtime_project, ..
                } => {
                    let id: String = self
                        .db
                        .query_row(
                            "SELECT id FROM deployments WHERE runtime_project_id=?1",
                            [runtime_project.to_string()],
                            |r| r.get(0),
                        )
                        .optional()?
                        .ok_or_else(|| missing("legacy runtime project"))?;
                    let id = super::parse(&id)?;
                    let old = self.deployment(id)?;
                    if !old.legacy || old.definition_id != config.id {
                        return Err(conflict(
                            "adoption requires a legacy runtime and its original public definition UUID",
                        ));
                    }
                    self.db.execute(
                        "UPDATE deployments SET legacy=0,target=?1,revision=revision+1 WHERE id=?2",
                        params![inspection.target, id.to_string()],
                    )?;
                    id
                }
                _ => unreachable!(),
            };
            let context = self.deployment(id)?;
            self.attach(source, format, &context)?;
            self.db.execute(
                "INSERT INTO binding_operations VALUES (?1,?2,?3,?4,?5,?5)",
                params![config.id.to_string(), key, request, id.to_string(), actor],
            )?;
            Ok(serde_json::to_value(context)?)
        })();
        self.finish_binding(result)
    }
}
