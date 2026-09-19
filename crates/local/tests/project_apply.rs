use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::PathBuf,
    process::{Child, Command as Process, Stdio},
    time::{Duration, Instant},
};
use supabricks_core::resource::{OperationId, ProjectId};
use supabricks_local::{
    api::{Action, Binding},
    deployments::{Command as D, Context, Source},
    environments::Manager,
    project_apply::{self as apply, Command, Operation, Options, Plan},
    store::Store,
};
struct Fixture {
    temp: tempfile::TempDir,
    root: PathBuf,
    path: PathBuf,
    binding: Binding,
    ctx: Context,
}
impl Fixture {
    fn new() -> (Self, Store) {
        let temp = tempfile::Builder::new()
            .prefix("sb-pk04-")
            .tempdir_in("/tmp")
            .unwrap();
        let root = temp.path().join("data");
        let path = temp.path().join("source");
        fs::create_dir(&path).unwrap();
        fs::create_dir(path.join("queries")).unwrap();
        fs::write(path.join("queries/total.sql"), "SELECT 42").unwrap();
        fs::write(path.join("supabricks.toml"),format!("format_version=2\nid='{}'\nname='example'\n[package]\nversion='0.1.0'\ninclude=['queries/total.sql']\nnotebook_outputs='strip'\n[resources.database.main]\nkind='postgres_database'\nlifecycle='retain'\n[resources.query.total]\nkind='sql'\nengine='postgres'\nfile='queries/total.sql'\ndatabase='database.main'\n",ProjectId::new())).unwrap();
        let path = path.canonicalize().unwrap();
        let mut store = Store::open(&root).unwrap();
        let ctx: Context = serde_json::from_value(
            store
                .project_command(
                    &Source::read(&path).unwrap(),
                    D::Create {
                        key: "create".into(),
                        target: None,
                    },
                )
                .unwrap(),
        )
        .unwrap();
        let binding = ctx.binding(&path);
        (
            Self {
                temp,
                root,
                path,
                binding,
                ctx,
            },
            store,
        )
    }
    fn plan(&self, s: &Store) -> Plan {
        apply::plan(s, &self.binding, Options::default()).unwrap()
    }
    fn apply(&self, s: &mut Store, p: Plan, key: &str) -> Operation {
        serde_json::from_value(
            apply::handle(
                s,
                &self.binding,
                Command::Apply {
                    plan: p,
                    key: key.into(),
                },
            )
            .unwrap(),
        )
        .unwrap()
    }
    fn finish(&self, s: &mut Store, id: OperationId) -> Operation {
        let mut env = Manager::default();
        for _ in 0..24 {
            apply::tick(s, &mut env, None, &mut Default::default()).unwrap();
            complete(s);
            let o = s.project_apply(self.ctx.deployment_id, id).unwrap();
            if !o.pending() {
                return o;
            }
        }
        panic!("apply did not terminate")
    }
    fn asset(&self, s: &mut Store) -> Value {
        apply::handle(
            s,
            &self.binding,
            Command::Asset {
                logical: "query.total".into(),
            },
        )
        .unwrap()
    }
}
fn complete(s: &mut Store) {
    for op in s.pending().unwrap() {
        while let Some(t) = s.ticket(op.id).unwrap() {
            if t.step == supabricks_local::operations::Step::CaptureSuspend {
                s.capture_suspend_lsn(&t, "0/100".parse().unwrap()).unwrap();
            }
            s.checkpoint(&t, json!({})).unwrap();
        }
    }
}
#[test]
fn plans_are_read_only_exactly_bound_and_retries_recover_original_result() {
    let (f, mut s) = Fixture::new();
    let p = f.plan(&s);
    let p2 = f.plan(&s);
    assert_eq!(
        serde_json::to_value(&p).unwrap(),
        serde_json::to_value(&p2).unwrap()
    );
    assert!(s.branches().unwrap().is_empty());
    assert!(!f.root.join("project-revisions").exists());
    let mut forged = p.clone();
    forged.context.actor_id = ProjectId::new().to_string();
    assert!(
        apply::handle(
            &mut s,
            &f.binding,
            Command::Apply {
                plan: forged,
                key: "forged".into()
            }
        )
        .is_err()
    );
    fs::write(f.path.join("queries/total.sql"), "SELECT 43").unwrap();
    assert!(
        apply::handle(
            &mut s,
            &f.binding,
            Command::Apply {
                plan: p,
                key: "stale".into()
            }
        )
        .is_err()
    );
    let p = f.plan(&s);
    let o = f.apply(&mut s, p.clone(), "apply");
    assert_eq!(f.apply(&mut s, p.clone(), "apply").id, o.id);
    assert!(
        apply::handle(
            &mut s,
            &f.binding,
            Command::Apply {
                plan: p.clone(),
                key: "concurrent".into()
            }
        )
        .is_err()
    );
    let done = f.finish(&mut s, o.id);
    assert_eq!(done.state, "succeeded", "{:?}", done.error);
    assert_eq!(
        s.active_deployment(f.ctx.deployment_id).unwrap(),
        Some(o.id)
    );
    assert_eq!(s.branches().unwrap().len(), 1);
    fs::write(f.path.join("queries/total.sql"), "SELECT 44").unwrap();
    assert_eq!(f.apply(&mut s, p, "apply").id, o.id);
    assert_eq!(f.asset(&mut s)["content"], "SELECT 43");
    assert!(
        serde_json::from_value::<Command>(json!({"action":"plan","actor_id":"spoof"})).is_err()
    );
}
#[test]
fn adopted_databases_require_explicit_ids_and_revision_fences() {
    let (f, mut s) = Fixture::new();
    let r = create(&mut s, &f.binding, "manual");
    complete(&mut s);
    assert!(apply::plan(&s, &f.binding, Options::default()).is_err());
    let id = serde_json::from_value(r["branch_id"].clone()).unwrap();
    let options = Options {
        adopt: BTreeMap::from([("database.main".into(), id)]),
    };
    let p = apply::plan(&s, &f.binding, options.clone()).unwrap();
    assert_eq!(p.steps[0].action, "adopt");
    s.submit(
        f.binding.project_id,
        "suspend",
        supabricks_local::operations::Mutation::SetState {
            branch_id: id,
            expected_revision: 1,
            desired: supabricks_core::resource::DesiredState::Suspended,
        },
    )
    .unwrap();
    complete(&mut s);
    assert!(
        apply::handle(
            &mut s,
            &f.binding,
            Command::Apply {
                plan: p,
                key: "stale".into()
            }
        )
        .is_err()
    );
    let p = apply::plan(&s, &f.binding, options).unwrap();
    let o = f.apply(&mut s, p, "adopt");
    assert_eq!(f.finish(&mut s, o.id).state, "succeeded");
    assert_eq!(
        s.deployment_resources(f.ctx.deployment_id).unwrap()["database.main"].branch,
        Some(id)
    );
    assert_eq!(s.branches().unwrap().len(), 1);
}
#[test]
fn cancellation_retains_allocations_and_previous_revision_missing_declarations_never_delete() {
    let (f, mut s) = Fixture::new();
    let p = f.plan(&s);
    let o = f.apply(&mut s, p, "first");
    let mut env = Manager::default();
    apply::tick(&mut s, &mut env, None, &mut Default::default()).unwrap();
    apply::tick(&mut s, &mut env, None, &mut Default::default()).unwrap();
    assert_eq!(s.branches().unwrap().len(), 1);
    apply::handle(&mut s, &f.binding, Command::Cancel { id: o.id }).unwrap();
    apply::tick(&mut s, &mut env, None, &mut Default::default()).unwrap();
    assert_eq!(
        s.project_apply(f.ctx.deployment_id, o.id).unwrap().state,
        "cancelled"
    );
    assert_eq!(s.active_deployment(f.ctx.deployment_id).unwrap(), None);
    complete(&mut s);
    let p = f.plan(&s);
    assert_eq!(p.steps[0].action, "retain");
    let o = f.apply(&mut s, p, "second");
    assert_eq!(f.finish(&mut s, o.id).state, "succeeded");
    let manifest = fs::read_to_string(f.path.join("supabricks.toml")).unwrap();
    fs::write(
        f.path.join("supabricks.toml"),
        manifest.split("[resources.database.main]").next().unwrap(),
    )
    .unwrap();
    let p = f.plan(&s);
    assert_eq!(p.retained, vec!["database.main", "query.total"]);
    let next = f.apply(&mut s, p, "remove-declarations");
    assert_eq!(f.finish(&mut s, next.id).state, "succeeded");
    assert_eq!(s.branches().unwrap().len(), 1);
    assert_eq!(f.asset(&mut s)["content"], "SELECT 42");
}
#[test]
fn drafts_and_source_edits_cannot_modify_installed_assets_or_escape_the_checkout() {
    let (f, mut s) = Fixture::new();
    let p = f.plan(&s);
    let o = f.apply(&mut s, p, "first");
    assert_eq!(f.finish(&mut s, o.id).state, "succeeded");
    let command = |path: &str| Command::Draft {
        logical: "query.total".into(),
        path: path.into(),
    };
    apply::handle(&mut s, &f.binding, command("queries/draft.sql")).unwrap();
    fs::write(f.path.join("queries/draft.sql"), "SELECT 99").unwrap();
    assert!(apply::handle(&mut s, &f.binding, command("queries/draft.sql")).is_err());
    assert_eq!(f.asset(&mut s)["content"], "SELECT 42");
    assert!(apply::handle(&mut s, &f.binding, command("queries/../../escape.sql")).is_err());
    std::os::unix::fs::symlink(f.temp.path(), f.path.join("queries/escape")).unwrap();
    assert!(apply::handle(&mut s, &f.binding, command("queries/escape/escaped.sql")).is_err());
    assert!(!f.temp.path().join("escaped.sql").exists());
    let bytes = fs::read(o.directory(&s).join("source.sbproj")).unwrap();
    drop(s);
    let backup = f.temp.path().join("backup");
    supabricks_local::recovery::create(&f.root, &backup).unwrap();
    let restored = f.temp.path().join("restored");
    supabricks_local::recovery::restore(&backup, &restored).unwrap();
    let mut s = Store::open(&restored).unwrap();
    assert_eq!(
        s.active_deployment(f.ctx.deployment_id).unwrap(),
        Some(o.id)
    );
    assert_eq!(f.asset(&mut s)["content"], "SELECT 42");
    assert_eq!(
        bytes,
        fs::read(o.directory(&s).join("source.sbproj")).unwrap()
    );
}
#[test]
fn a_source_change_before_staging_fails_without_resources_or_activation() {
    let (f, mut s) = Fixture::new();
    let p = f.plan(&s);
    let o = f.apply(&mut s, p, "first");
    fs::write(f.path.join("queries/total.sql"), "SELECT 'changed'").unwrap();
    let done = f.finish(&mut s, o.id);
    assert_eq!(done.state, "failed");
    assert!(done.error.unwrap().contains("source changed"));
    assert!(s.branches().unwrap().is_empty());
    assert_eq!(s.active_deployment(f.ctx.deployment_id).unwrap(), None);
}
struct Daemon(Child);
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn start(root: &std::path::Path, point: Option<&str>) -> Daemon {
    let mut p = Process::new(env!("CARGO_BIN_EXE_supabricks"));
    p.args(["daemon", "--data-dir"])
        .arg(root)
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(point) = point {
        p.env("SUPABRICKS_TEST_PROJECT_APPLY_KILL", point);
    }
    Daemon(p.spawn().unwrap())
}
#[test]
fn sigkill_recovers_each_journal_boundary_without_duplicate_databases_or_partial_activation() {
    for point in [
        "intent",
        "archive",
        "staged",
        "database_submitted",
        "database_owned",
        "step",
        "prepared",
        "before_activation",
        "activation_transaction",
        "activated",
        "cancelled",
    ] {
        let (f, mut s) = Fixture::new();
        let creates = matches!(point, "database_submitted" | "database_owned");
        let options = if creates {
            Options::default()
        } else {
            let r = create(&mut s, &f.binding, "existing");
            complete(&mut s);
            Options {
                adopt: BTreeMap::from([(
                    "database.main".into(),
                    serde_json::from_value(r["branch_id"].clone()).unwrap(),
                )]),
            }
        };
        let p = apply::plan(&s, &f.binding, options).unwrap();
        let id = if point == "intent" {
            None
        } else {
            let o = f.apply(&mut s, p.clone(), "kill");
            if point == "cancelled" {
                apply::handle(&mut s, &f.binding, Command::Cancel { id: o.id }).unwrap();
            }
            Some(o.id)
        };
        drop(s);
        let mut d = start(&f.root, Some(point));
        let deadline = Instant::now() + Duration::from_secs(15);
        if point == "intent" {
            while supabricks_local::client::Client::bind(&f.root, &f.path).is_err() {
                assert!(Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(10));
            }
            let c = supabricks_local::client::Client::bind(&f.root, &f.path).unwrap();
            let _ = c.call(Action::ProjectApply {
                command: Command::Apply {
                    plan: p.clone(),
                    key: "kill".into(),
                },
            });
        }
        loop {
            if let Some(status) = d.0.try_wait().unwrap() {
                assert!(!status.success(), "{point}");
                break;
            }
            assert!(Instant::now() < deadline, "{point}");
            std::thread::sleep(Duration::from_millis(20));
        }
        let mut s = Store::open(&f.root).unwrap();
        let original = s.find_apply(f.ctx.deployment_id, "kill").unwrap().unwrap();
        assert!(id.is_none_or(|id| id == original.id));
        assert_eq!(
            s.active_deployment(f.ctx.deployment_id).unwrap(),
            if point == "activated" {
                Some(original.id)
            } else {
                None
            },
            "{point}"
        );
        complete(&mut s);
        let done = f.finish(&mut s, original.id);
        assert_eq!(
            done.state,
            if point == "cancelled" {
                "cancelled"
            } else {
                "succeeded"
            },
            "{point}: {:?}",
            done.error
        );
        assert_eq!(s.branches().unwrap().len(), 1, "{point}");
        assert_eq!(f.apply(&mut s, p, "kill").id, original.id, "{point}");
    }
}

fn create(s: &mut Store, b: &Binding, key: &str) -> Value {
    json!(
        s.submit(
            b.project_id,
            key,
            supabricks_local::operations::Mutation::CreateDatabase {
                name: "main".into(),
                ports: supabricks_local::operations::Ports {
                    sql: 54101,
                    external_http: 54102,
                    internal_http: 54103
                }
            }
        )
        .unwrap()
    )
}

#[test]
fn cli_and_fixed_worktree_mcp_share_the_plan_and_strict_contract() {
    let (f, s) = Fixture::new();
    drop(s);
    let _daemon = start(&f.root, None);
    let deadline = Instant::now() + Duration::from_secs(5);
    while supabricks_local::client::Client::bind(&f.root, &f.path).is_err() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let output = Process::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["project", "plan", "--data-dir"])
        .arg(&f.root)
        .arg("--project")
        .arg(&f.path)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let plan: Value = serde_json::from_slice(&output.stdout).unwrap();
    let client = supabricks_local::client::Client::bind_source(&f.root, &f.path).unwrap();
    let mut mcp = supabricks_local::mcp::Session::default();
    mcp.dispatch(&client,json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"pk04","version":"1"}}})).unwrap();
    mcp.dispatch(
        &client,
        json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
    );
    let result=mcp.dispatch(&client,json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"project_plan","arguments":{}}})).unwrap();
    assert_eq!(result["result"]["structuredContent"], plan);
    let error=mcp.dispatch(&client,json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"project_plan","arguments":{"worktree":"/another"}}})).unwrap();
    assert_eq!(error["error"]["code"], -32602);
    let mut fixture = plan;
    for (i, key) in [
        "definition_id",
        "deployment_id",
        "runtime_project_id",
        "workspace_id",
        "realm_id",
        "actor_id",
    ]
    .iter()
    .enumerate()
    {
        fixture["context"][key] = json!(format!("00000000-0000-4000-8000-{:012}", i + 1));
    }
    fixture["context"]["effective_principal_id"] = fixture["context"]["actor_id"].clone();
    fixture["worktree"] = json!("/project");
    for key in ["source_sha256", "archive_sha256", "content_sha256"] {
        fixture[key] = json!("a".repeat(64));
    }
    fixture["digest"] = json!("");
    fixture.sort_all_objects();
    use sha2::{Digest, Sha256};
    fixture["digest"] = json!(hex::encode(Sha256::digest(
        serde_json::to_vec(&fixture).unwrap()
    )));
    let encoded = serde_json::to_string_pretty(&fixture).unwrap();
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/project-plan.json"
    );
    if std::env::var_os("UPDATE_LOCAL_SNAPSHOTS").is_some() {
        fs::write(path, &encoded).unwrap();
    }
    assert_eq!(encoded, fs::read_to_string(path).unwrap());
}

#[test]
fn journal_write_failure_is_reported_without_stopping_the_daemon() {
    let (f, s) = Fixture::new();
    let p = f.plan(&s);
    drop(s);
    let mut daemon = start(&f.root, None);
    let deadline = Instant::now() + Duration::from_secs(5);
    while supabricks_local::client::Client::bind(&f.root, &f.path).is_err() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let db = rusqlite::Connection::open(f.root.join("state.sqlite3")).unwrap();
    db.execute_batch("CREATE TRIGGER fail_apply_update BEFORE UPDATE ON project_applies BEGIN SELECT RAISE(ABORT, 'injected journal write failure'); END;").unwrap();
    let client = supabricks_local::client::Client::bind(&f.root, &f.path).unwrap();
    let result = client
        .call(Action::ProjectApply {
            command: Command::Apply {
                plan: p,
                key: "write-failure".into(),
            },
        })
        .unwrap();
    let id = serde_json::from_value(result["id"].clone()).unwrap();
    loop {
        let status =
            supabricks_local::client::request(&f.root, supabricks_local::daemon::Request::Status)
                .unwrap();
        if !status["project_apply_error"].is_null() {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(daemon.0.try_wait().unwrap().is_none());
    db.execute_batch("DROP TRIGGER fail_apply_update").unwrap();
    client
        .call(Action::ProjectApply {
            command: Command::Cancel { id },
        })
        .unwrap();
    loop {
        let status = client
            .call(Action::ProjectApply {
                command: Command::Status { id },
            })
            .unwrap();
        if status["state"] == "cancelled" {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        client
            .call(Action::ProjectApply {
                command: Command::Installed
            })
            .unwrap()["active_revision"],
        Value::Null
    );
}

#[test]
fn killed_environment_submission_recovers_as_failed_without_activating_partial_state() {
    use sha2::{Digest, Sha256};
    let (f, mut s) = Fixture::new();
    let py = b"[project]\nname='test'\nversion='0.1.0'\n";
    let lock = b"version=1\npackage=[]\n";
    let source = f.path.join("notebooks/environment");
    fs::create_dir_all(&source).unwrap();
    fs::write(source.join("pyproject.toml"), py).unwrap();
    fs::write(source.join("uv.lock"), lock).unwrap();
    let manifest = f.path.join("supabricks.toml");
    let mut text=fs::read_to_string(&manifest).unwrap().replace("include=['queries/total.sql']","include=['queries/total.sql','notebooks/environment/pyproject.toml','notebooks/environment/uv.lock']");
    text += "\n[environments.base]\npyproject='notebooks/environment/pyproject.toml'\nlock='notebooks/environment/uv.lock'\n";
    fs::write(manifest, text).unwrap();
    let package = f.temp.path().join("package");
    fs::create_dir_all(package.join("python/analytics")).unwrap();
    fs::create_dir_all(package.join("python/notebooks")).unwrap();
    fs::create_dir_all(package.join("python/runtime/bin")).unwrap();
    fs::write(package.join("python/analytics/export.py"), b"").unwrap();
    fs::write(
        f.root.join("analytics.json"),
        serde_json::to_vec(
            &json!({"python":"/bin/sh","worker":package.join("python/analytics/export.py")}),
        )
        .unwrap(),
    )
    .unwrap();
    let mut files = BTreeMap::new();
    for (path, bytes) in [
        (
            "python/runtime/bin/python3.12",
            b"qualified interpreter".as_slice(),
        ),
        ("python/notebooks/pyproject.toml", py.as_slice()),
        ("python/notebooks/uv.lock", lock.as_slice()),
        (
            "python/notebooks/requirements.lock",
            b"requirements".as_slice(),
        ),
    ] {
        fs::write(package.join(path), bytes).unwrap();
        files.insert(path, hex::encode(Sha256::digest(bytes)));
    }
    fs::write(package.join("python/notebooks/kernel-contract.json"),serde_json::to_vec(&json!({"version":1,"target":if cfg!(target_os="linux"){"linux-x86_64"}else{"macos-arm64"},"python_version":"3.12.13","files":files,"templates":{"base":{"manifest":"python/notebooks/pyproject.toml","lock":"python/notebooks/uv.lock","requirements":"python/notebooks/requirements.lock","inputs":{"manifest":hex::encode(Sha256::digest(py)),"lock":hex::encode(Sha256::digest(lock))},"packages":{}}}})).unwrap()).unwrap();
    let p = f.plan(&s);
    let o = f.apply(&mut s, p, "environment-crash");
    drop(s);
    let mut daemon = start(&f.root, Some("environment_submitted"));
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = daemon.0.try_wait().unwrap() {
            assert!(!status.success());
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    let mut s = Store::open(&f.root).unwrap();
    let mut environments = Manager::recover(&mut s).unwrap();
    apply::tick(&mut s, &mut environments, None, &mut Default::default()).unwrap();
    let failed = s.project_apply(f.ctx.deployment_id, o.id).unwrap();
    assert_eq!(failed.state, "failed");
    assert!(
        failed
            .error
            .unwrap()
            .contains("preparation did not complete")
    );
    assert_eq!(s.active_deployment(f.ctx.deployment_id).unwrap(), None);
    assert!(s.branches().unwrap().is_empty());
}

#[test]
fn initialization_plan_orders_checksums_and_rejects_implicit_adoption() {
    let (f, mut s) = Fixture::new();
    fs::write(
        f.path.join("queries/first.sql"),
        "CREATE TABLE public.marker(id integer)",
    )
    .unwrap();
    fs::write(
        f.path.join("queries/second.sql"),
        "INSERT INTO public.marker VALUES (1)",
    )
    .unwrap();
    let manifest = f.path.join("supabricks.toml");
    let source = fs::read_to_string(&manifest)
        .unwrap()
        .replace("['queries/total.sql']", "['queries/*.sql']")
        + "\n[resources.migration.z_first]\nkind='migration'\nfile='queries/first.sql'\ndatabase='database.main'\nsequence=1\n[resources.migration.a_second]\nkind='migration'\nfile='queries/second.sql'\ndatabase='database.main'\nsequence=2\n";
    fs::write(&manifest, &source).unwrap();
    let p = f.plan(&s);
    let migrations: Vec<_> = p.steps.iter().filter(|s| s.kind == "migration").collect();
    assert_eq!(
        migrations
            .iter()
            .map(|s| s.logical.as_str())
            .collect::<Vec<_>>(),
        vec!["migration.z_first", "migration.a_second"]
    );
    assert_eq!(
        migrations[0].initialization.as_ref().unwrap()["sha256"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
    assert!(
        s.list_branches(f.binding.project_id, false)
            .unwrap()
            .is_empty()
    );
    fs::write(&manifest, source.replace("sequence=2", "sequence=1")).unwrap();
    assert!(apply::plan(&s, &f.binding, Options::default()).is_err());
    fs::write(&manifest, &source).unwrap();
    let result = create(&mut s, &f.binding, "external");
    let branch = serde_json::from_value(result["branch_id"].clone()).unwrap();
    complete(&mut s);
    let options = Options {
        adopt: BTreeMap::from([("database.main".into(), branch)]),
    };
    assert!(
        apply::plan(&s, &f.binding, options)
            .unwrap_err()
            .to_string()
            .contains("separate reviewed apply")
    );
    fs::write(f.path.join("queries/first.sql"), "COMMIT").unwrap();
    assert!(apply::plan(&s, &f.binding, Options::default()).is_err());
}

#[test]
fn pending_project_apply_protects_database_even_from_force_delete() {
    let (f, mut s) = Fixture::new();
    let plan = f.plan(&s);
    let o = f.apply(&mut s, plan, "protect");
    let mut env = Manager::default();
    let mut workers = Default::default();
    apply::tick(&mut s, &mut env, None, &mut workers).unwrap();
    apply::tick(&mut s, &mut env, None, &mut workers).unwrap();
    complete(&mut s);
    let branch = s
        .project_apply(f.ctx.deployment_id, o.id)
        .unwrap()
        .resources["database.main"]
        .branch
        .unwrap();
    assert!(
        s.submit(
            f.binding.project_id,
            "force",
            supabricks_local::operations::Mutation::ForceDelete {
                branch_id: branch,
                expected_revision: 1
            }
        )
        .unwrap_err()
        .to_string()
        .contains("pending project apply")
    );
    apply::handle(&mut s, &f.binding, Command::Cancel { id: o.id }).unwrap();
    apply::tick(&mut s, &mut env, None, &mut workers).unwrap();
    assert!(
        s.submit(
            f.binding.project_id,
            "force",
            supabricks_local::operations::Mutation::ForceDelete {
                branch_id: branch,
                expected_revision: 1
            }
        )
        .is_ok()
    );
}

#[test]
fn database_retryable_error_keeps_apply_pending_but_terminal_error_fails() {
    for terminal in [false, true] {
        let (f, mut s) = Fixture::new();
        let plan = f.plan(&s);
        let operation = f.apply(&mut s, plan, "retryable-database");
        let mut env = Manager::default();
        for _ in 0..2 {
            apply::tick(&mut s, &mut env, None, &mut Default::default()).unwrap();
        }
        let child = s.pending().unwrap().into_iter().next().unwrap();
        s.operation_error(
            child.id,
            json!({"code":"unavailable","retryable":!terminal}),
            terminal,
        )
        .unwrap();
        apply::tick(&mut s, &mut env, None, &mut Default::default()).unwrap();
        let observed = s.project_apply(f.ctx.deployment_id, operation.id).unwrap();
        assert_eq!(s.active_deployment(f.ctx.deployment_id).unwrap(), None);
        if terminal {
            assert_eq!(observed.state, "failed");
        } else {
            assert!(observed.pending());
            complete(&mut s);
            assert_eq!(f.finish(&mut s, operation.id).state, "succeeded");
        }
        assert_eq!(s.branches().unwrap().len(), 1);
    }
}
