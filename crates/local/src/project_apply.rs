//! PK04: destination-bound plans and durable, retain-only deployment application.
use crate::{
    api::Binding,
    deployments::Context,
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
mod fixtures;
pub mod migrations;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, path::PathBuf};
use supabricks_core::resource::{BranchId, OperationId};

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Options {
    /// Explicit adoption of existing branches. Names never imply adoption.
    #[serde(default)]
    pub adopt: BTreeMap<String, BranchId>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Plan {
        #[serde(default)]
        options: Options,
    },
    Apply {
        plan: Plan,
        key: String,
    },
    Status {
        id: OperationId,
    },
    Find {
        key: String,
    },
    Cancel {
        id: OperationId,
    },
    Installed,
    Asset {
        logical: String,
    },
    Draft {
        logical: String,
        path: String,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub api_version: u32,
    pub digest: String,
    pub context: Context,
    pub worktree: PathBuf,
    pub source_sha256: String,
    pub archive_sha256: String,
    pub content_sha256: String,
    pub installation: Option<String>,
    pub options: Options,
    pub previous: Option<OperationId>,
    pub steps: Vec<Step>,
    pub retained: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub dependency_closure: BTreeMap<String, Value>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    pub logical: String,
    pub kind: String,
    pub action: String,
    pub branch: Option<BranchId>,
    pub expected_revision: Option<i64>,
    pub file: Option<String>,
    pub database: Option<String>,
    pub environment: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initialization: Option<Value>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Resource {
    pub kind: String,
    pub origin: OperationId,
    pub branch: Option<BranchId>,
    pub file: Option<String>,
    pub database: Option<String>,
    pub environment: Option<String>,
    pub generation: Option<OperationId>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub receipt: Option<Value>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Operation {
    pub api_version: u32,
    pub id: OperationId,
    pub key: String,
    pub plan: Plan,
    pub state: String,
    pub next_step: usize,
    pub cancel_requested: bool,
    pub resources: BTreeMap<String, Resource>,
    pub error: Option<String>,
}
impl Operation {
    pub fn pending(&self) -> bool {
        matches!(self.state.as_str(), "queued" | "preparing" | "activating")
    }
    pub fn directory(&self, store: &Store) -> PathBuf {
        store
            .root()
            .join("project-revisions")
            .join(self.plan.context.deployment_id.to_string())
            .join(self.id.to_string())
    }
}
pub(crate) fn digest(value: &impl Serialize) -> Result<String> {
    let mut v = serde_json::to_value(value)?;
    v.sort_all_objects();
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(&v)?)))
}
pub fn plan(store: &Store, binding: &Binding, options: Options) -> Result<Plan> {
    let context = store.binding_context(binding)?;
    if context.legacy {
        return Err(conflict("adopt the format-2 project before planning"));
    }
    let prepared = crate::projects::package::prepare(&binding.worktree, Some(&context.target))?;
    let report = &prepared.report.inspection;
    if report.definition.id != context.definition_id {
        return Err(conflict("definition changed during planning"));
    }
    let owned = store.deployment_resources(context.deployment_id)?;
    let branches = store.list_branches(context.runtime_project_id, false)?;
    let mut steps = Vec::new();
    let mut dependency_closure = BTreeMap::new();
    for (name, input) in &report.environments {
        if !input.bundles.is_empty() {
            let package = crate::environments::Package::load(store)?;
            let bundle = input
                .bundles
                .get(&package.target)
                .ok_or_else(|| conflict("project has no wheel bundle for this native target"))?;
            if bundle.kernel_contract != package.identity {
                return Err(conflict(
                    "project wheel bundle requires another kernel contract",
                ));
            }
            dependency_closure.insert(name.clone(), json!({"target":package.target,"kernel_contract":package.identity,"targets":input.bundles,"status":"prepare_offline_before_activation"}));
        }
        steps.push(Step {
            logical: format!("environment.{name}"),
            kind: "environment".into(),
            action: "prepare_offline".into(),
            branch: None,
            expected_revision: None,
            file: None,
            database: None,
            environment: Some(name.clone()),
            initialization: None,
        });
    }
    for logical in &report.order {
        use crate::projects::manifest::Resource as R;
        let mut step = Step {
            logical: logical.clone(),
            kind: String::new(),
            action: "install".into(),
            branch: None,
            expected_revision: None,
            file: None,
            database: None,
            environment: None,
            initialization: None,
        };
        match &report.resources[logical].declaration {
            R::PostgresDatabase { .. } => {
                step.kind = "database".into();
                let existing = owned.get(logical).and_then(|r| r.branch);
                let adopt = options.adopt.get(logical).copied();
                if existing.is_some() && adopt.is_some() && existing != adopt {
                    return Err(conflict("an owned database cannot be rebound"));
                }
                step.branch = existing.or(adopt);
                if let Some(id) = step.branch {
                    let b = branches.iter().find(|b| b.branch.id == id).ok_or_else(|| {
                        conflict("planned database is missing or belongs to another deployment")
                    })?;
                    store.accepting_work(id)?;
                    if b.branch.parent_id.is_some() {
                        return Err(invalid("database adoption requires a root database"));
                    }
                    if owned
                        .iter()
                        .any(|(k, r)| k != logical && r.branch == Some(id))
                        || options.adopt.iter().any(|(k, b)| k != logical && *b == id)
                    {
                        return Err(conflict(
                            "database already belongs to another logical resource",
                        ));
                    }
                    step.expected_revision = Some(b.revision);
                    step.action = if existing.is_some() {
                        "retain"
                    } else {
                        "adopt"
                    }
                    .into();
                } else {
                    let name = logical.strip_prefix("database.").unwrap();
                    if branches.iter().any(|b| b.branch.name == name) {
                        return Err(conflict(
                            "database name already exists; explicitly adopt its branch UUID",
                        ));
                    }
                    step.action = "create".into();
                }
            }
            R::Migration {
                file,
                database,
                sequence,
                ..
            } => {
                step.kind = "migration".into();
                step.action = "transactional_migration".into();
                step.file = Some(file.clone());
                step.database = Some(database.clone());
                step.initialization = Some(
                    json!({"sequence":sequence,"sha256":report.files[file].sha256,"transaction":"one_statement_and_receipt"}),
                );
            }
            R::Fixture {
                file,
                database,
                schema,
                table,
                mapping,
                ..
            } => {
                step.kind = "fixture".into();
                step.action = "load_new_table".into();
                step.file = Some(file.clone());
                step.database = Some(database.clone());
                step.initialization = Some(
                    json!({"sha256":report.files[file].sha256,"schema":schema,"table":table,"mapping":mapping}),
                );
            }
            R::Sql {
                file,
                database,
                engine,
                ..
            } => {
                step.kind = match engine {
                    crate::projects::manifest::Engine::Postgres => "postgres_query",
                    _ => "spark_query",
                }
                .into();
                step.file = Some(file.clone());
                step.database = Some(database.clone());
            }
            R::Notebook {
                file,
                database,
                environment,
                ..
            } => {
                step.kind = "notebook".into();
                step.file = Some(file.clone());
                step.database = Some(database.clone());
                step.environment = Some(environment.clone());
            }
        }
        steps.push(step);
    }
    if options.adopt.keys().any(|k| {
        !steps
            .iter()
            .any(|s| s.logical == *k && s.kind == "database")
    }) {
        return Err(invalid("adoption must reference a declared database"));
    }
    for step in &steps {
        if matches!(step.kind.as_str(), "migration" | "fixture") {
            let database = steps
                .iter()
                .find(|s| Some(&s.logical) == step.database.as_ref())
                .unwrap();
            if database.action == "adopt" {
                return Err(conflict(
                    "initialization requires a fresh or already owned database; adopt in a separate reviewed apply first",
                ));
            }
            if let Some(id) = database.branch {
                let b = store.branch(id)?;
                if b.endpoint.desired_state != supabricks_core::resource::DesiredState::Running
                    || b.observed_revision != b.revision
                {
                    return Err(conflict(
                        "resume the owned database and wait before planning initialization",
                    ));
                }
            }
        }
    }
    let retained = owned
        .keys()
        .filter(|k| !steps.iter().any(|s| &s.logical == *k))
        .cloned()
        .collect();
    let mut result = Plan {
        api_version: 1,
        digest: String::new(),
        worktree: binding.worktree.clone(),
        source_sha256: prepared.source_sha256,
        archive_sha256: prepared.report.archive_sha256,
        content_sha256: prepared.report.content_sha256,
        installation: crate::installation::Installation::discover()?.map(|i| i.identity),
        options,
        previous: store.active_deployment(context.deployment_id)?,
        context,
        steps,
        retained,
        dependency_closure,
    };
    result.digest = digest(&result)?;
    if serde_json::to_vec(&result)?.len() > 48 * 1024 {
        return Err(invalid(
            "project plan exceeds 48 KiB; reduce resource declarations",
        ));
    }
    Ok(result)
}
pub fn handle(store: &mut Store, binding: &Binding, command: Command) -> Result<Value> {
    let context = store.binding_context(binding)?;
    match command {
        Command::Plan { options } => Ok(json!(plan(store, binding, options)?)),
        Command::Apply {
            plan: requested,
            key,
        } => {
            crate::notebooks::contract::key(&key)?;
            if requested.context.deployment_id != context.deployment_id
                || requested.worktree != binding.worktree
            {
                return Err(conflict("plan belongs to another deployment or worktree"));
            }
            if let Some(old) = store.find_apply(context.deployment_id, &key)? {
                if digest(&old.plan)? != digest(&requested)? {
                    return Err(conflict("apply key was used for another plan"));
                }
                return Ok(json!(old)); // Lost replies recover the original even after activation/source edits.
            }
            let current = plan(store, binding, requested.options.clone())?;
            if digest(&current)? != digest(&requested)? {
                return Err(conflict("plan is stale or modified; plan again"));
            }
            let op = Operation {
                api_version: 1,
                id: OperationId::new(),
                key,
                plan: current,
                state: "queued".into(),
                next_step: 0,
                cancel_requested: false,
                resources: BTreeMap::new(),
                error: None,
            };
            store.insert_apply(&op)?;
            checkpoint("intent");
            Ok(json!(op))
        }
        Command::Find { key } => {
            Ok(json!({"operation":store.find_apply(context.deployment_id,&key)?}))
        }
        Command::Status { id } | Command::Cancel { id } => {
            let mut o = store.project_apply(context.deployment_id, id)?;
            if matches!(command, Command::Cancel { .. }) && o.pending() {
                o.cancel_requested = true;
                store.save_apply(&o)?;
            }
            Ok(json!(o))
        }
        Command::Installed => {
            let resources = store.deployment_resources(context.deployment_id)?;
            let mut worktrees = BTreeMap::new();
            let mut preparation_needed = false;
            for (logical, r) in &resources {
                if r.kind == "environment" {
                    let op = store.project_apply(context.deployment_id, r.origin)?;
                    let path = op
                        .directory(store)
                        .join("environments")
                        .join(r.environment.as_ref().unwrap());
                    preparation_needed |= r.generation.is_none_or(|id| {
                        crate::environments::Manager::select(store, &context.binding(&path), id)
                            .is_err()
                    });
                    worktrees.insert(logical.clone(), path);
                }
            }
            Ok(
                json!({"api_version":1,"context":context,"active_revision":store.active_deployment(context.deployment_id)?,"resources":resources,"environment_worktrees":worktrees,"preparation_needed":preparation_needed}),
            )
        }
        Command::Asset { ref logical } | Command::Draft { ref logical, .. } => {
            let logical = logical.clone();
            let resource = store
                .deployment_resources(context.deployment_id)?
                .get(&logical)
                .cloned()
                .ok_or_else(|| invalid("installed asset not found"))?;
            let file = resource
                .file
                .as_ref()
                .ok_or_else(|| invalid("resource is not a source asset"))?;
            let op = store.project_apply(context.deployment_id, resource.origin)?;
            let (report, files) = crate::projects::package::read(
                &op.directory(store).join("source.sbproj"),
                Some(&op.plan.context.target),
            )?;
            if report.archive_sha256 != op.plan.archive_sha256 {
                return Err(conflict("installed source archive changed"));
            }
            let bytes = files
                .get(file)
                .ok_or_else(|| invalid("installed file missing"))?;
            if let Command::Draft { path, .. } = command {
                crate::projects::validate_draft_path(&path, resource.kind == "notebook")?;
                crate::projects::publication::write_draft(&binding.worktree, &path, bytes)?;
                Ok(
                    json!({"api_version":1,"path":path,"draft":true,"origin_revision":resource.origin,"logical":logical}),
                )
            } else {
                if serde_json::to_vec(bytes)?.len() > 1024 * 1024 {
                    return Err(invalid(
                        "asset exceeds response limit; use project draft to copy it locally",
                    ));
                }
                Ok(
                    json!({"api_version":1,"logical":logical,"revision":resource.origin,"read_only":true,"resource":resource,"content":std::str::from_utf8(bytes).map_err(|_|invalid("asset must be UTF-8"))?}),
                )
            }
        }
    }
}
// The failpoint is intentionally limited to an explicitly configured test daemon.
pub(crate) fn checkpoint(name: &str) {
    if std::env::var("SUPABRICKS_TEST_PROJECT_APPLY_KILL")
        .ok()
        .as_deref()
        == Some(name)
    {
        unsafe {
            libc::raise(libc::SIGKILL);
        }
    }
}

/// Each call performs at most one adapter checkpoint per deployment. Child effects
/// use stable keys in the existing PostgreSQL and environment journals.
pub fn tick(
    store: &mut Store,
    environments: &mut crate::environments::Manager,
    cell: Option<&crate::engine::Cell>,
    migrations: &mut migrations::Workers,
) -> Result<()> {
    for mut o in store.pending_applies()? {
        if let Err(e) = advance(store, environments, cell, migrations, &mut o) {
            if let Some(step) = o.plan.steps.get(o.next_step) {
                let key = format!("{}:{}", o.id, step.logical);
                if migrations.contains(&key) {
                    let _ = migrations.finish(&key);
                    if migrations.contains(&key) {
                        continue;
                    }
                }
            }
            o.state = "failed".into();
            o.error = Some(format!(
                "step {}: {e}; previous active revision is retained. Earlier committed initialization remains. Inspect receipts and make a new plan.",
                o.plan
                    .steps
                    .get(o.next_step)
                    .map_or("activation", |s| s.logical.as_str())
            ));
            store.save_apply(&o)?;
        }
    }
    Ok(())
}
fn private_dir(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt};
    if !path.exists() {
        std::fs::DirBuilder::new().mode(0o700).create(path)?;
        std::fs::File::open(path.parent().unwrap())?.sync_all()?;
    }
    let m = std::fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } || m.mode() & 0o077 != 0 {
        return Err(conflict(
            "project revision directory is not private and owned",
        ));
    }
    Ok(())
}
fn stage(store: &Store, o: &Operation) -> Result<()> {
    let root = store.root().join("project-revisions");
    private_dir(&root)?;
    let parent = root.join(o.plan.context.deployment_id.to_string());
    private_dir(&parent)?;
    let dir = o.directory(store);
    private_dir(&dir)?;
    let archive = dir.join("source.sbproj");
    if !archive.exists() {
        let prepared =
            crate::projects::package::prepare(&o.plan.worktree, Some(&o.plan.context.target))?;
        if prepared.source_sha256 != o.plan.source_sha256
            || prepared.report.archive_sha256 != o.plan.archive_sha256
        {
            return Err(conflict("planned source changed before staging"));
        }
        crate::projects::publication::write_new(&archive, &prepared.archive)?;
    }
    if crate::projects::package::verify(&archive, Some(&o.plan.context.target))?.archive_sha256
        != o.plan.archive_sha256
    {
        return Err(conflict("staged archive changed"));
    }
    checkpoint("archive");
    Ok(())
}
fn env_binding(store: &mut Store, o: &Operation, name: &str) -> Result<Binding> {
    let path = o.directory(store).join("environments").join(name);
    let source = crate::deployments::Source::read(&path)?;
    store.project_command(
        &source,
        crate::deployments::Command::Attach {
            deployment: o.plan.context.deployment_id,
        },
    )?;
    Ok(o.plan.context.binding(&path))
}
fn prepare_environment(
    store: &mut Store,
    environments: &mut crate::environments::Manager,
    o: &Operation,
    name: &str,
) -> Result<Value> {
    let path = o.directory(store).join("environments").join(name);
    let key = format!("project:{}:{name}", o.id);
    if path.exists() {
        let binding = env_binding(store, o, name)?;
        if let Some(found) = environments
            .handle(
                store,
                &binding,
                crate::environments::Command::Find { key: key.clone() },
            )?
            .get("operation")
            .filter(|v| !v.is_null())
        {
            return Ok(found.clone());
        }
    }
    let (report, files) = crate::projects::package::read(
        &o.directory(store).join("source.sbproj"),
        Some(&o.plan.context.target),
    )?;
    if report.archive_sha256 != o.plan.archive_sha256 {
        return Err(conflict("staged archive changed"));
    }
    let input = &report.inspection.environments[name];
    let parent = o.directory(store).join("environments");
    private_dir(&parent)?;
    let path = parent.join(name);
    if !path.exists() {
        let p = crate::projects::publication::Publication::new(&path)?;
        // A destination-owned working root for the existing environment service.
        // Only source files in source.sbproj are immutable installed content.
        let config = crate::project::ProjectConfig {
            format_version: 1,
            id: o.plan.context.definition_id,
            name: report.inspection.definition.name,
        };
        p.write(
            "supabricks.toml",
            toml::to_string(&config)
                .map_err(|_| invalid("environment identity encoding"))?
                .as_bytes(),
        )?;
        p.write(
            "notebooks/environment/pyproject.toml",
            &files[&input.pyproject],
        )?;
        p.write("notebooks/environment/uv.lock", &files[&input.lock])?;
        if !input.bundles.is_empty() {
            let package = crate::environments::Package::load(store)?;
            let bundle = input
                .bundles
                .get(&package.target)
                .ok_or_else(|| conflict("missing target bundle"))?;
            if bundle.kernel_contract != package.identity {
                return Err(conflict("kernel contract changed"));
            }
            p.write("dependency.zip", &files[&bundle.path])?;
        }
        for (logical, node) in &report.inspection.resources {
            if let crate::projects::manifest::Resource::Notebook {
                file, environment, ..
            } = &node.declaration
            {
                if environment == name {
                    p.write(
                        &format!(
                            "notebooks/{}.ipynb",
                            logical.strip_prefix("notebook.").unwrap()
                        ),
                        &files[file],
                    )?;
                }
            }
        }
        p.publish_directory()?;
    }
    let binding = env_binding(store, o, name)?;
    let key = format!("project:{}:{name}", o.id);
    if let Some(found) = environments
        .handle(
            store,
            &binding,
            crate::environments::Command::Find { key: key.clone() },
        )?
        .get("operation")
        .filter(|v| !v.is_null())
    {
        return Ok(found.clone());
    }
    let expected = crate::environments::Inputs {
        manifest: input.pyproject_sha256.clone(),
        lock: input.lock_sha256.clone(),
    };
    let command = if input.bundles.is_empty() {
        crate::environments::Command::Prepare { key, expected }
    } else {
        crate::environments::Command::Manage {
            key,
            expected,
            offline: true,
            change: crate::environments::Change::ImportBundle {
                path: path.join("dependency.zip"),
            },
        }
    };
    environments.handle(store, &binding, command)
}
fn advance(
    store: &mut Store,
    environments: &mut crate::environments::Manager,
    cell: Option<&crate::engine::Cell>,
    migrations: &mut migrations::Workers,
    o: &mut Operation,
) -> Result<()> {
    let ctx = store.binding_context(&o.plan.context.binding(&o.plan.worktree))?;
    if ctx.revision != o.plan.context.revision || ctx.deployment_id != o.plan.context.deployment_id
    {
        return Err(conflict(
            "deployment binding changed while apply was pending",
        ));
    }
    if crate::installation::Installation::discover()?.map(|i| i.identity) != o.plan.installation {
        return Err(conflict("installation changed during apply"));
    }
    // Recover a database allocation whose child journal committed just before SIGKILL.
    if let Some(step) = o
        .plan
        .steps
        .get(o.next_step)
        .filter(|s| s.action == "create")
    {
        let key = format!("project:{}:{}", o.id, step.logical);
        if store
            .request_for_key(ctx.runtime_project_id, &key)?
            .is_some()
        {
            record_database(store, cell, o, step.clone())?;
        }
    }
    if o.cancel_requested {
        if let Some(step) = o.plan.steps.get(o.next_step) {
            let key = format!("{}:{}", o.id, step.logical);
            if migrations.contains(&key) {
                // Finish receipt reconciliation before acknowledging cancellation.
                let Some(receipt) = initialization(store, migrations, o, step)? else {
                    return Ok(());
                };
                o.resources.insert(
                    step.logical.clone(),
                    Resource {
                        kind: step.kind.clone(),
                        origin: o.id,
                        branch: step.branch,
                        file: step.file.clone(),
                        database: step.database.clone(),
                        environment: None,
                        generation: None,
                        receipt: Some(receipt),
                    },
                );
            }
        }
        if !fixtures::cancel(store, o)? {
            return Ok(());
        }
        for env in store
            .environment_operations()?
            .into_iter()
            .filter(|e| e.key.starts_with(&format!("project:{}:", o.id)))
        {
            let binding = ctx.binding(&env.worktree);
            environments.handle(
                store,
                &binding,
                crate::environments::Command::Cancel { id: env.id },
            )?;
        }
        o.state = "cancelled".into();
        store.save_apply(o)?;
        checkpoint("cancelled");
        return Ok(());
    }
    if o.state == "queued" {
        stage(store, o)?;
        o.state = "preparing".into();
        store.save_apply(o)?;
        checkpoint("staged");
        return Ok(());
    }
    if let Some(step) = o.plan.steps.get(o.next_step).cloned() {
        let mut resource = Resource {
            kind: step.kind.clone(),
            origin: o.id,
            branch: step.branch,
            file: step.file.clone(),
            database: step.database.clone(),
            environment: step.environment.clone(),
            generation: None,
            receipt: None,
        };
        if step.kind == "database" {
            if step.action == "create" {
                if !record_database(store, cell, o, step.clone())? {
                    return Ok(());
                }
                resource = o.resources[&step.logical].clone();
            } else {
                let id = step.branch.unwrap();
                store.accepting_work(id)?;
                if Some(store.branch(id)?.revision) != step.expected_revision {
                    return Err(conflict("database changed after planning"));
                }
                store.own_database(ctx.deployment_id, &step.logical, &resource)?;
            }
        } else if matches!(step.kind.as_str(), "migration" | "fixture") {
            let Some(receipt) = initialization(store, migrations, o, &step)? else {
                return Ok(());
            };
            resource.receipt = Some(receipt);
        } else if step.kind == "environment" {
            let status =
                prepare_environment(store, environments, o, step.environment.as_ref().unwrap())?;
            checkpoint("environment_submitted");
            match status["state"].as_str() {
                Some("ready") => {
                    resource.generation =
                        Some(serde_json::from_value(status["generation"].clone())?)
                }
                Some("failed" | "cancelled") => {
                    return Err(conflict(format!(
                        "offline environment preparation did not complete: {}",
                        status["error"]
                    )));
                }
                _ => return Ok(()),
            }
        }
        o.resources.insert(step.logical.clone(), resource);
        o.next_step += 1;
        store.save_apply(o)?;
        checkpoint("step");
        return Ok(());
    }
    if o.state != "activating" {
        o.state = "activating".into();
        store.save_apply(o)?;
        checkpoint("prepared");
        return Ok(());
    }
    // Revalidate every external prerequisite immediately before the atomic pointer change.
    stage(store, o)?;
    for step in &o.plan.steps {
        if step.kind == "database" {
            let id = o.resources[&step.logical].branch.unwrap();
            store.accepting_work(id)?;
            let want = step.expected_revision.unwrap_or(1);
            let branch = store.branch(id)?;
            if branch.revision != want
                || branch.observed_revision != want
                || !branch.timeline_created
            {
                return Err(conflict("database revision changed before activation"));
            }
        } else if step.kind == "environment" {
            let binding = env_binding(store, o, step.environment.as_ref().unwrap())?;
            let selected = crate::environments::Manager::select(
                store,
                &binding,
                o.resources[&step.logical].generation.unwrap(),
            )?;
            let report = crate::projects::package::verify(
                &o.directory(store).join("source.sbproj"),
                Some(&o.plan.context.target),
            )?;
            let expected = &report.inspection.environments[step.environment.as_ref().unwrap()];
            if selected.identity.inputs
                != (crate::environments::Inputs {
                    manifest: expected.pyproject_sha256.clone(),
                    lock: expected.lock_sha256.clone(),
                })
            {
                return Err(conflict(
                    "prepared environment differs from the reviewed source declarations",
                ));
            }
        }
    }
    checkpoint("before_activation");
    store.activate_deployment(o)?;
    checkpoint("activated");
    Ok(())
}
fn record_database(
    store: &mut Store,
    cell: Option<&crate::engine::Cell>,
    o: &mut Operation,
    step: Step,
) -> Result<bool> {
    let result = crate::api::handle(
        store,
        cell,
        &o.plan.context.binding(&o.plan.worktree),
        crate::api::Action::CreateDatabase {
            name: step.logical.strip_prefix("database.").unwrap().into(),
            key: format!("project:{}:{}", o.id, step.logical),
        },
    )?;
    let child: crate::operations::Operation = serde_json::from_value(result)?;
    checkpoint("database_submitted");
    let r = Resource {
        kind: "database".into(),
        origin: o.id,
        branch: Some(child.branch_id),
        file: None,
        database: None,
        environment: None,
        generation: None,
        receipt: None,
    };
    store.own_database(o.plan.context.deployment_id, &step.logical, &r)?;
    o.resources.insert(step.logical, r);
    store.save_apply(o)?;
    checkpoint("database_owned");
    match child.status {
        crate::operations::Status::Succeeded => Ok(true),
        crate::operations::Status::Pending if child.error.is_none() => Ok(false),
        _ => Err(conflict(
            "database preparation failed or was superseded; inspect the retained branch",
        )),
    }
}

fn initialization(
    store: &mut Store,
    workers: &mut migrations::Workers,
    o: &Operation,
    step: &Step,
) -> Result<Option<Value>> {
    let worker_key = format!("{}:{}", o.id, step.logical);
    if workers.contains(&worker_key) {
        return workers.finish(&worker_key);
    }
    let database = o
        .resources
        .get(step.database.as_ref().unwrap())
        .ok_or_else(|| conflict("initialization database was not prepared"))?;
    let id = database.branch.unwrap();
    store.accepting_work(id)?;
    let b = store.branch(id)?;
    let expected = o
        .plan
        .steps
        .iter()
        .find(|s| s.logical == *step.database.as_ref().unwrap())
        .unwrap()
        .expected_revision
        .unwrap_or(1);
    if b.revision != expected
        || b.observed_revision != expected
        || b.endpoint.desired_state != supabricks_core::resource::DesiredState::Running
    {
        return Err(conflict(
            "initialization database changed or is not running",
        ));
    }
    if step.kind == "fixture" && fixtures::exists(store, o, step, id)? {
        return fixtures::load(store, o, step, id, &[]);
    }
    let (report, files) = crate::projects::package::read(
        &o.directory(store).join("source.sbproj"),
        Some(&o.plan.context.target),
    )?;
    if report.archive_sha256 != o.plan.archive_sha256 {
        return Err(conflict("initialization archive changed"));
    }
    let bytes = &files[step.file.as_ref().unwrap()];
    let config = step.initialization.as_ref().unwrap();
    if hex::encode(Sha256::digest(bytes)) != config["sha256"].as_str().unwrap() {
        return Err(conflict("initialization checksum changed"));
    }
    if step.kind == "fixture" {
        return fixtures::load(store, o, step, id, bytes);
    }
    workers.poll(
        &format!("{}:{}", o.id, step.logical),
        migrations::Request {
            port: b
                .ports
                .ok_or_else(|| conflict("migration database has no compute port"))?
                .sql,
            password: store.endpoint_password(b.endpoint.id)?,
            origin: store.ingest_origin()?,
            deployment: o.plan.context.deployment_id.to_string(),
            branch: id.to_string(),
            logical: step.logical.clone(),
            sequence: config["sequence"].as_i64().unwrap(),
            sha256: config["sha256"].as_str().unwrap().into(),
            sql: std::str::from_utf8(bytes)
                .map_err(|_| invalid("migration UTF-8"))?
                .into(),
        },
    )
}
