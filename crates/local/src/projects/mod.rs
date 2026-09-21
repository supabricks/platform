//! PK01: pure, bounded project inspection. No daemon, catalog, subprocess or network.
pub mod bundles;
pub mod data;
pub mod manifest;
pub mod package;
pub(crate) mod publication;
pub(crate) mod source;
use crate::{
    project::ProjectConfig,
    store::{
        Result,
        error::{conflict, invalid},
    },
};
use manifest::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use source::Source;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};
use supabricks_core::resource::ProjectId;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    ProjectInspect { target: Option<String> },
    ProjectValidate { target: Option<String> },
}
#[derive(Debug, Serialize)]
pub struct Definition {
    pub format_version: u32,
    pub id: ProjectId,
    pub name: String,
}
#[derive(Debug, Serialize)]
pub struct Node {
    pub declaration: Resource,
    pub dependencies: Vec<String>,
}
#[derive(Debug, Serialize)]
pub struct EnvironmentInput {
    pub pyproject: String,
    pub lock: String,
    pub pyproject_sha256: String,
    pub lock_sha256: String,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub bundles: BTreeMap<String, bundles::Closure>,
}
#[derive(Debug, Serialize)]
pub struct UnresolvedBinding {
    pub resource: String,
    pub kind: String,
}
#[derive(Debug, Serialize)]
pub struct Inspection {
    pub api_version: u32,
    pub schema_version: u32,
    pub preview: bool,
    pub valid: bool,
    pub definition: Definition,
    pub target: String,
    pub targets: BTreeMap<String, Target>,
    pub package: Option<Package>,
    pub capabilities: Vec<String>,
    pub resources: BTreeMap<String, Node>,
    pub order: Vec<String>,
    pub environments: BTreeMap<String, EnvironmentInput>,
    pub files: BTreeMap<String, source::Entry>,
    pub unresolved_bindings: Vec<UnresolvedBinding>,
    pub source_sha256: String,
    pub execution_supported: bool,
    pub limitations: Vec<String>,
}
const CAPABILITIES: &[&str] = &[
    "postgres17",
    "spark-sql",
    "managed-notebooks",
    crate::catalog::datasets::CAPABILITY,
];

/// Establish only public source identity for a fixed MCP session. Does not admit execution.
pub fn source_identity(directory: &Path) -> Result<ProjectConfig> {
    Ok(source_identity_version(directory)?.0)
}
pub(crate) fn source_identity_version(directory: &Path) -> Result<(ProjectConfig, u32)> {
    let mut source = Source::new(directory)?;
    let bytes = source.read("supabricks.toml")?;
    if bytes.len() > 256 * 1024 {
        return Err(invalid("supabricks.toml exceeds 256 KiB"));
    }
    let version = version(&bytes)?;
    let config = if version == 1 {
        let c: ProjectConfig = parse(&bytes, "supabricks.toml")?;
        c.validate()?;
        c
    } else {
        let m: Manifest = parse(&bytes, "supabricks.toml")?;
        identity(m.id, &m.name)?
    };
    source.verify()?;
    Ok((config, version))
}
pub fn execute(directory: &Path, command: Command) -> Result<Inspection> {
    let target = match command {
        Command::ProjectInspect { target } | Command::ProjectValidate { target } => target,
    };
    inspect(directory, target.as_deref())
}
pub fn inspect(directory: &Path, target: Option<&str>) -> Result<Inspection> {
    let mut source = Source::new(directory)?;
    inspect_inputs(&mut source, target)
}
pub(crate) fn inspect_inputs(source: &mut Source, target: Option<&str>) -> Result<Inspection> {
    let bytes = source.read("supabricks.toml")?;
    if bytes.len() > 256 * 1024 {
        return Err(invalid("supabricks.toml exceeds 256 KiB"));
    }
    let version = version(&bytes)?;
    let (id, name, mut model) = if version == 1 {
        let config: ProjectConfig = parse(&bytes, "supabricks.toml")?;
        config.validate()?;
        (config.id, config.name, None)
    } else {
        let model: Manifest = parse(&bytes, "supabricks.toml")?;
        identity(model.id, &model.name)?;
        semver::Version::parse(&model.package.version)
            .map_err(|_| invalid("package.version must be a semantic version"))?;
        reject_interpolation(&serde_json::to_value(&model)?)?;
        (model.id, model.name.clone(), Some(model))
    };
    let mut resources = BTreeMap::new();
    let mut environments = BTreeMap::new();
    let mut capabilities = BTreeSet::new();
    let mut unresolved_bindings = Vec::new();
    let mut targets = BTreeMap::new();
    if let Some(model) = &mut model {
        if model.package.include.len() > 128
            || model.targets.len() > 32
            || model.environments.len() > 32
            || model.requires.capabilities.len() > 32
        {
            return Err(invalid("project declaration exceeds PK01 count limits"));
        }
        let mut included = BTreeSet::new();
        let mut fragment_paths = BTreeSet::new();
        for name in model.include.clone() {
            fragment(source, &name, model, &mut fragment_paths, 0)?;
        }
        for pattern in &model.package.include {
            if !included.insert(pattern.clone()) {
                return Err(invalid("duplicate package include pattern"));
            }
            source.expand(pattern)?;
        }
        for cap in &model.requires.capabilities {
            if !CAPABILITIES.contains(&cap.as_str()) {
                return Err(invalid(format!("unsupported required capability: {cap}")));
            }
            if !capabilities.insert(cap.clone()) {
                return Err(invalid("duplicate required capability"));
            }
        }
        for (group, entries) in &model.resources {
            if !matches!(
                group.as_str(),
                "database" | "query" | "notebook" | "migration" | "fixture" | "dataset"
            ) {
                return Err(invalid("unsupported resource group"));
            }
            for (name, declaration) in entries {
                key(name)?;
                if declaration.group() != group {
                    return Err(invalid("resource kind does not match its group"));
                }
                if resources.len() >= 256 {
                    return Err(invalid("project exceeds 256 resources"));
                }
                let logical = format!("{group}.{name}");
                match declaration {
                    Resource::CatalogDataset {
                        requirement,
                        expected_schema_sha256,
                        expected_content_sha256,
                        ..
                    } => {
                        if requirement.is_empty()
                            || requirement.len() > 256
                            || requirement.chars().any(char::is_control)
                        {
                            return Err(invalid(
                                "dataset requirement must contain 1–256 printable bytes",
                            ));
                        }
                        for hash in [expected_schema_sha256, expected_content_sha256]
                            .into_iter()
                            .flatten()
                        {
                            if hash.len() != 64
                                || !hash
                                    .bytes()
                                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                            {
                                return Err(invalid(
                                    "dataset fingerprints must be lowercase SHA-256",
                                ));
                            }
                        }
                        if entries.len() > crate::catalog::datasets::MAX_DATASETS {
                            return Err(invalid("project exceeds eight dataset bindings"));
                        }
                        capabilities.insert(crate::catalog::datasets::CAPABILITY.into());
                        capabilities.insert("spark-sql".into());
                        unresolved_bindings.push(UnresolvedBinding {
                            resource: logical.clone(),
                            kind: "catalog_dataset".into(),
                        });
                    }

                    Resource::PostgresDatabase { .. } => {
                        capabilities.insert("postgres17".into());
                        unresolved_bindings.push(UnresolvedBinding {
                            resource: logical.clone(),
                            kind: "destination_database".into(),
                        });
                    }
                    Resource::Migration {
                        file,
                        database,
                        sequence,
                        ..
                    } => {
                        require_file(source, file)?;
                        if !file.ends_with(".sql") || *sequence == 0 {
                            return Err(invalid(
                                "migration needs a .sql file and positive sequence",
                            ));
                        }
                        crate::project_apply::migrations::validate(&source.read(file)?)?;
                        database_ref(database, model)?;
                    }
                    Resource::Fixture {
                        file,
                        database,
                        schema,
                        table,
                        mapping,
                        ..
                    } => {
                        require_file(source, file)?;
                        database_ref(database, model)?;
                        mapping.fingerprint()?;
                        if mapping.format == crate::ingest::Format::Csv {
                            let positions: BTreeSet<_> =
                                mapping.columns.iter().map(|c| c.input.as_str()).collect();
                            if !(0..mapping.columns.len())
                                .all(|i| positions.contains(i.to_string().as_str()))
                            {
                                return Err(invalid(
                                    "CSV fixture mapping inputs must cover zero-based column positions",
                                ));
                            }
                        }
                        crate::ingest::identifier(schema)?;
                        crate::ingest::identifier(table)?;
                        if schema == "_supabricks"
                            || schema.starts_with("pg_")
                            || schema == "information_schema"
                        {
                            return Err(invalid("fixture destination is reserved"));
                        }
                    }
                    Resource::Sql {
                        engine,
                        file,
                        database,
                        ..
                    } => {
                        require_file(source, file)?;
                        if !file.ends_with(".sql") {
                            return Err(invalid("SQL resources require a .sql file"));
                        }
                        database_ref(database, model)?;
                        capabilities.insert(
                            match engine {
                                Engine::Postgres => "postgres17",
                                Engine::Spark => "spark-sql",
                            }
                            .into(),
                        );
                    }
                    Resource::Notebook {
                        file,
                        environment,
                        database,
                        ..
                    } => {
                        require_file(source, file)?;
                        if !file.ends_with(".ipynb") {
                            return Err(invalid("notebook resources require an .ipynb file"));
                        }
                        let doc: serde_json::Value = serde_json::from_slice(&source.read(file)?)
                            .map_err(|_| invalid("invalid notebook JSON"))?;
                        crate::notebooks::files::validate_document(&doc)?;
                        if !model.environments.contains_key(environment) {
                            return Err(invalid("notebook references an undeclared environment"));
                        }
                        database_ref(database, model)?;
                        capabilities.insert("managed-notebooks".into());
                        capabilities.insert("spark-sql".into());
                    }
                }
                let dependencies = declaration.dependencies();
                if dependencies.len() > 256 {
                    return Err(invalid("resource has too many dependencies"));
                }
                resources.insert(
                    logical,
                    Node {
                        declaration: declaration.clone(),
                        dependencies,
                    },
                );
            }
        }
        for (name, env) in &model.environments {
            key(name)?;
            require_file(source, &env.pyproject)?;
            require_file(source, &env.lock)?;
            let (py_parent, py_name) = env
                .pyproject
                .rsplit_once('/')
                .unwrap_or(("", &env.pyproject));
            let (lock_parent, lock_name) = env.lock.rsplit_once('/').unwrap_or(("", &env.lock));
            if py_name != "pyproject.toml" || lock_name != "uv.lock" || py_parent != lock_parent {
                return Err(invalid(
                    "environment requires sibling project-contained pyproject.toml and uv.lock",
                ));
            }
            let py: toml::Value = parse(&source.read(&env.pyproject)?, &env.pyproject)?;
            if !py.get("project").is_some_and(toml::Value::is_table) {
                return Err(invalid(
                    "environment pyproject.toml requires a project table",
                ));
            }
            let lock: toml::Value = parse(&source.read(&env.lock)?, &env.lock)?;
            if lock.get("version").and_then(toml::Value::as_integer) != Some(1)
                || !lock.get("package").is_some_and(toml::Value::is_array)
            {
                return Err(invalid("expected uv lock version 1 with a package array"));
            }
            let mut bundles = BTreeMap::new();
            for (target, path) in &env.bundles {
                if !matches!(target.as_str(), "linux-x86_64" | "macos-arm64")
                    || !source::is_bundle(path)
                {
                    return Err(invalid(
                        "bundles require a native target and dependencies/*.zip path",
                    ));
                }
                require_file(source, path)?;
                let closure = bundles::inspect(
                    path,
                    &source.read(path)?,
                    target,
                    &source.files[&env.pyproject].sha256,
                    &source.files[&env.lock].sha256,
                )?;
                bundles.insert(target.clone(), closure);
            }
            environments.insert(
                name.clone(),
                EnvironmentInput {
                    pyproject: env.pyproject.clone(),
                    lock: env.lock.clone(),
                    pyproject_sha256: source.files[&env.pyproject].sha256.clone(),
                    lock_sha256: source.files[&env.lock].sha256.clone(),
                    bundles,
                },
            );
        }
        targets = model.targets.clone();
    }
    if targets.is_empty() {
        targets.insert(
            "local".into(),
            Target {
                mode: Mode::Development,
                default: true,
            },
        );
    }
    for name in targets.keys() {
        key(name)?;
    }
    let defaults: Vec<_> = targets
        .iter()
        .filter(|(_, t)| t.default)
        .map(|(k, _)| k.clone())
        .collect();
    if defaults.len() > 1 {
        return Err(invalid("only one target may be default"));
    }
    let target = match target {
        Some(name) if targets.contains_key(name) => name.to_owned(),
        Some(_) => return Err(invalid("unknown project target")),
        None if defaults.len() == 1 => defaults[0].clone(),
        None if targets.len() == 1 => targets.keys().next().unwrap().clone(),
        None => {
            return Err(invalid(
                "multiple targets require --target or one explicit default",
            ));
        }
    };
    // Sequence ordering is per database, independent of lexical resource names.
    let mut migrations = BTreeMap::<String, BTreeMap<u32, String>>::new();
    let mut destinations = BTreeSet::new();
    for (logical, node) in &resources {
        match &node.declaration {
            Resource::Migration {
                database, sequence, ..
            } => {
                if migrations
                    .entry(database.clone())
                    .or_default()
                    .insert(*sequence, logical.clone())
                    .is_some()
                {
                    return Err(invalid("duplicate migration sequence in a database"));
                }
            }
            Resource::Fixture {
                database,
                schema,
                table,
                ..
            } => {
                if !destinations.insert((database, schema, table)) {
                    return Err(invalid("fixtures must have distinct destination tables"));
                }
            }
            _ => (),
        }
    }
    for sequence in migrations.values() {
        let mut previous = None;
        for logical in sequence.values() {
            if let Some(parent) = previous {
                let deps = &mut resources.get_mut(logical).unwrap().dependencies;
                deps.push(parent);
                deps.sort();
                deps.dedup();
            }
            previous = Some(logical.clone());
        }
    }
    let order = order(&resources)?;
    source.verify()?;
    let mut report = Inspection {api_version: 1, schema_version: 1, preview: true, valid: true,
        definition: Definition {format_version: version, id, name}, target, targets,
        package: model.map(|m|m.package), capabilities: capabilities.into_iter().collect(), resources, order,
        environments, files: source.files.clone(), unresolved_bindings, source_sha256: String::new(), execution_supported: version == 1,
        limitations: vec!["Source inspection only; no package, deployment or catalog access is created.".into(), "Dependency hashes do not establish uv lock freshness, kernel compatibility or offline readiness; preparation validates those later.".into(), "File inventory is not a guarantee that arbitrary source contains no secrets.".into()]};
    report.source_sha256 = hex::encode(Sha256::digest(serde_json::to_vec(&report.files)?));
    Ok(report)
}
fn require_file(source: &mut Source, name: &str) -> Result<()> {
    source::path(name, false)?;
    if !source.files.contains_key(name) {
        return Err(invalid(format!(
            "resource/dependency file must be selected by package.include: {name}"
        )));
    }
    source.read(name)?;
    Ok(())
}
fn database_ref(name: &str, model: &Manifest) -> Result<()> {
    let Some(key) = name.strip_prefix("database.") else {
        return Err(invalid("database reference must use database.<key>"));
    };
    if !model
        .resources
        .get("database")
        .is_some_and(|v| v.contains_key(key))
    {
        return Err(invalid("undeclared database resource reference"));
    }
    Ok(())
}
fn fragment(
    source: &mut Source,
    name: &str,
    model: &mut Manifest,
    visited: &mut BTreeSet<String>,
    depth: usize,
) -> Result<()> {
    if depth >= 4 || visited.len() >= 32 {
        return Err(invalid("resource includes exceed depth 4 or 32 fragments"));
    }
    if !visited.insert(name.into()) {
        return Err(invalid("duplicate or cyclic resource include"));
    }
    if !name.starts_with("resources/") || !name.ends_with(".toml") {
        return Err(invalid(
            "resource includes must be explicit resources/*.toml paths",
        ));
    }
    let bytes = source.read(name)?;
    if bytes.len() > 256 * 1024 {
        return Err(invalid("resource fragment exceeds 256 KiB"));
    }
    let value: toml::Value = parse(&bytes, name)?;
    reject_interpolation(&serde_json::to_value(value)?)?;
    let data: Fragment = parse(&bytes, name)?;
    for (group, entries) in data.resources {
        let dest = model.resources.entry(group).or_default();
        for (key, resource) in entries {
            if dest.insert(key, resource).is_some() {
                return Err(conflict(
                    "resource key is declared more than once across project fragments",
                ));
            }
        }
    }
    for child in data.include {
        fragment(source, &child, model, visited, depth + 1)?;
    }
    Ok(())
}
fn reject_interpolation(value: &serde_json::Value) -> Result<()> {
    match value {
        serde_json::Value::String(s)
            if s.contains("${") || s.contains("{{") || s.contains("$(") =>
        {
            return Err(invalid(
                "project configuration interpolation is unsupported; use literal declarations",
            ));
        }
        serde_json::Value::Array(a) => {
            for v in a {
                reject_interpolation(v)?;
            }
        }
        serde_json::Value::Object(o) => {
            for (k, v) in o {
                reject_interpolation(&serde_json::Value::String(k.clone()))?;
                reject_interpolation(v)?;
            }
        }
        _ => (),
    }
    Ok(())
}
fn order(nodes: &BTreeMap<String, Node>) -> Result<Vec<String>> {
    fn visit(
        key: &str,
        nodes: &BTreeMap<String, Node>,
        visiting: &mut BTreeSet<String>,
        done: &mut BTreeSet<String>,
        output: &mut Vec<String>,
    ) -> Result<()> {
        if done.contains(key) {
            return Ok(());
        }
        if !visiting.insert(key.into()) {
            return Err(invalid("resource dependency graph contains a cycle"));
        }
        let node = nodes
            .get(key)
            .ok_or_else(|| invalid(format!("undeclared resource dependency: {key}")))?;
        for dependency in &node.dependencies {
            visit(dependency, nodes, visiting, done, output)?;
        }
        visiting.remove(key);
        done.insert(key.into());
        output.push(key.into());
        Ok(())
    }
    let (mut visiting, mut done, mut output) = (BTreeSet::new(), BTreeSet::new(), Vec::new());
    for key in nodes.keys() {
        visit(key, nodes, &mut visiting, &mut done, &mut output)?;
    }
    Ok(output)
}

pub(crate) fn validate_draft_path(path: &str, notebook: bool) -> Result<()> {
    source::path(path, false)?;
    if notebook {
        let relative = path
            .strip_prefix("notebooks/")
            .ok_or_else(|| invalid("notebook drafts belong under notebooks/"))?;
        crate::notebooks::files::relative_path(relative)?;
    } else if !path.starts_with("queries/") || !path.ends_with(".sql") {
        return Err(invalid(
            "query drafts belong under queries/ with a .sql extension",
        ));
    }
    Ok(())
}
