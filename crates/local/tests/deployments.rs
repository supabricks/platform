use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Child, Command as Process, Stdio},
    time::{Duration, Instant},
};
use supabricks_local::{
    api::{Action, Binding},
    client::{Client, request},
    daemon::Request,
    deployments::Command,
    project::ProjectConfig,
};
struct Fixture {
    temp: tempfile::TempDir,
    root: PathBuf,
    daemon: Option<Child>,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(mut d) = self.daemon.take() {
            let _ = d.kill();
            let _ = d.wait();
        }
    }
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::Builder::new()
            .prefix("sb-pk03-")
            .tempdir_in("/tmp")
            .unwrap();
        let root = temp.path().join("data");
        let mut f = Self {
            temp,
            root,
            daemon: None,
        };
        f.start();
        f
    }
    fn start(&mut self) {
        self.daemon = Some(
            Process::new(env!("CARGO_BIN_EXE_supabricks"))
                .args(["daemon", "--data-dir"])
                .arg(&self.root)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        while request(&self.root, Request::Status).is_err() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn restart(&mut self) {
        let mut d = self.daemon.take().unwrap();
        d.kill().unwrap();
        d.wait().unwrap();
        self.start();
    }
    fn source(&self, name: &str, id: &str) -> PathBuf {
        let dir = self.temp.path().join(name);
        fs::create_dir(&dir).unwrap();
        fs::write(dir.join("supabricks.toml"),format!("format_version=2\nid='{id}'\nname='example'\n[package]\nversion='0.1.0'\ninclude=[]\nnotebook_outputs='strip'\n[targets.local]\nmode='development'\ndefault=true\n[targets.staging]\nmode='production'\n")).unwrap();
        dir.canonicalize().unwrap()
    }
    fn client(&self, path: &Path) -> Client {
        Client::bind_source(&self.root, path).unwrap()
    }
    fn create(&self, path: &Path, key: &str) -> Value {
        self.client(path)
            .project(Command::Create {
                key: key.into(),
                target: None,
            })
            .unwrap()
    }
}
fn id(v: &Value) -> supabricks_core::resource::DeploymentId {
    serde_json::from_value(v["deployment_id"].clone()).unwrap()
}
fn runtime(v: &Value) -> supabricks_core::resource::ProjectId {
    serde_json::from_value(v["runtime_project_id"].clone()).unwrap()
}
fn database(client: &Client, key: &str) -> Value {
    client
        .call(Action::CreateDatabase {
            name: "main".into(),
            key: key.into(),
        })
        .unwrap()
}
#[test]
fn copied_definition_deploys_twice_with_isolated_resources_and_durable_owner_context() {
    let mut f = Fixture::new();
    let definition = supabricks_core::resource::ProjectId::new().to_string();
    let a = f.source("one", &definition);
    let b = f.source("two", &definition);
    assert!(Client::bind(&f.root, &a).is_err());
    let one = f.create(&a, "one");
    assert!(Client::bind(&f.root, &b).is_err());
    let two = f.create(&b, "two");
    assert_eq!(one["definition_id"], two["definition_id"]);
    assert_ne!(one["runtime_project_id"], two["runtime_project_id"]);
    assert_ne!(one["deployment_id"], two["deployment_id"]);
    assert_eq!(one["actor_id"], one["effective_principal_id"]);
    assert_eq!(one["workspace_id"], two["workspace_id"]);
    assert_eq!(one["identity_provider"], "local-owner");
    let mut contract = one.clone();
    for (index, key) in [
        "definition_id",
        "deployment_id",
        "runtime_project_id",
        "workspace_id",
        "realm_id",
        "actor_id",
        "effective_principal_id",
    ]
    .iter()
    .enumerate()
    {
        contract[*key] = json!(format!(
            "00000000-0000-0000-0000-{:012}",
            if *key == "effective_principal_id" {
                6
            } else {
                index + 1
            }
        ));
    }
    let fixture =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/deployment-context.json");
    let contract = serde_json::to_string_pretty(&contract).unwrap() + "\n";
    if std::env::var_os("UPDATE_LOCAL_SNAPSHOTS").is_some() {
        fs::write(&fixture, &contract).unwrap();
    }
    assert_eq!(contract, fs::read_to_string(fixture).unwrap());

    let ca = Client::bind(&f.root, &a).unwrap();
    let cb = Client::bind(&f.root, &b).unwrap();
    let overview = request(
        &f.root,
        Request::ConsoleOverview {
            binding: ca.binding.clone(),
            generation: request(&f.root, Request::Status).unwrap()["generation"]
                .as_i64()
                .unwrap(),
        },
    )
    .unwrap();
    assert_eq!(overview["project"]["id"], one["runtime_project_id"]);
    assert_eq!(overview["definition_id"], one["definition_id"]);
    assert_eq!(overview["deployment"], one);

    let first = database(&ca, "same-key");
    let second = database(&cb, "same-key");
    assert_ne!(first["branch_id"], second["branch_id"]);
    let ba = ca
        .call(Action::GetBranch {
            branch: first["branch_id"].as_str().unwrap().into(),
        })
        .unwrap();
    let bb = cb
        .call(Action::GetBranch {
            branch: second["branch_id"].as_str().unwrap().into(),
        })
        .unwrap();
    assert_ne!(ba["branch"]["tenant_id"], bb["branch"]["tenant_id"]);
    assert!(
        cb.call(Action::GetBranch {
            branch: first["branch_id"].as_str().unwrap().into()
        })
        .is_err()
    );
    assert_eq!(one, f.create(&a, "one"));
    assert!(
        f.client(&a)
            .project(Command::Create {
                key: "changed".into(),
                target: None
            })
            .is_err()
    );
    f.restart();
    assert_eq!(one, f.client(&a).project(Command::Inspect).unwrap());
    assert_eq!(two, f.client(&b).project(Command::Inspect).unwrap());
}
#[test]
fn explicit_attach_shares_resources_but_keeps_branch_selections_separate() {
    let f = Fixture::new();
    let definition = supabricks_core::resource::ProjectId::new().to_string();
    let a = f.source("a", &definition);
    let b = f.source("b", &definition);
    let first = f.create(&a, "first");
    let ca = Client::bind(&f.root, &a).unwrap();
    let branch = database(&ca, "main")["branch_id"]
        .as_str()
        .unwrap()
        .to_owned();
    ca.call(Action::SelectBranch {
        branch: branch.clone(),
    })
    .unwrap();
    assert!(Client::bind(&f.root, &b).is_err());
    assert_eq!(
        f.client(&b)
            .project(Command::Attach {
                deployment: id(&first)
            })
            .unwrap(),
        first
    );
    let cb = Client::bind(&f.root, &b).unwrap();
    assert!(cb.call(Action::Selection).is_err());
    cb.call(Action::SelectBranch {
        branch: branch.clone(),
    })
    .unwrap();
    assert_eq!(cb.call(Action::Selection).unwrap()["branch_id"], branch);
    let other = f.source(
        "other",
        &supabricks_core::resource::ProjectId::new().to_string(),
    );
    assert!(
        f.client(&other)
            .project(Command::Attach {
                deployment: id(&first)
            })
            .is_err()
    );
}
#[test]
fn legacy_source_requires_explicit_adoption_and_preserves_runtime_and_saved_files() {
    let mut f = Fixture::new();
    let path = f.temp.path().join("legacy");
    fs::create_dir(&path).unwrap();
    let config = ProjectConfig::initialize(&path, "legacy").unwrap();
    let legacy = Client::bind(&f.root, &path).unwrap();
    let binding = legacy.project(Command::Inspect).unwrap();
    let branch = database(&legacy, "main")["branch_id"]
        .as_str()
        .unwrap()
        .to_owned();
    legacy
        .call(Action::SelectBranch {
            branch: branch.clone(),
        })
        .unwrap();
    let queries = f.root.join("queries").join(config.id.to_string());
    fs::create_dir_all(&queries).unwrap();
    fs::write(queries.join("kept.json"), b"private revision bytes").unwrap();
    let copy = f.source("copy", &config.id.to_string());
    assert!(Client::bind(&f.root, &copy).is_err());
    assert!(
        f.client(&copy)
            .project(Command::Attach {
                deployment: id(&binding)
            })
            .is_err()
    );
    fs::copy(copy.join("supabricks.toml"), path.join("supabricks.toml")).unwrap();
    assert!(Client::bind(&f.root, &path).is_err());
    let adopt = || Command::Adopt {
        runtime_project: config.id,
        key: "adopt".into(),
        target: None,
    };
    let adopted = f.client(&path).project(adopt()).unwrap();
    assert_eq!(adopted["runtime_project_id"], config.id.to_string());
    assert_eq!(adopted["deployment_id"], binding["deployment_id"]);
    assert_eq!(adopted["revision"], 2);
    assert_eq!(adopted["legacy"], false);
    assert_eq!(f.client(&path).project(adopt()).unwrap(), adopted);
    assert_eq!(
        Client::bind(&f.root, &path)
            .unwrap()
            .call(Action::Selection)
            .unwrap()["branch_id"],
        branch
    );
    assert_eq!(
        fs::read(queries.join("kept.json")).unwrap(),
        b"private revision bytes"
    );
    f.restart();
    assert_eq!(f.client(&path).project(Command::Inspect).unwrap(), adopted);
}
#[test]
fn directory_replacement_rename_and_label_change_never_retarget_implicitly() {
    let f = Fixture::new();
    let definition = supabricks_core::resource::ProjectId::new().to_string();
    let path = f.source("original", &definition);
    let deployment = f.create(&path, "new");
    let renamed = f.temp.path().join("renamed");
    fs::rename(&path, &renamed).unwrap();
    assert!(Client::bind(&f.root, &renamed).is_err());
    f.client(&renamed)
        .project(Command::Attach {
            deployment: id(&deployment),
        })
        .unwrap();
    let manifest = renamed.join("supabricks.toml");
    fs::write(
        &manifest,
        fs::read_to_string(&manifest)
            .unwrap()
            .replace("name='example'", "name='renamed'"),
    )
    .unwrap();
    assert_eq!(
        runtime(&f.client(&renamed).project(Command::Inspect).unwrap()),
        runtime(&deployment)
    );
    let replacement = f.source("original", &definition);
    assert!(Client::bind(&f.root, &replacement).is_err());
    f.client(&replacement)
        .project(Command::Attach {
            deployment: id(&deployment),
        })
        .unwrap();
    assert_eq!(
        Client::bind(&f.root, &replacement)
            .unwrap()
            .binding
            .project_id,
        runtime(&deployment)
    );
}
#[test]
fn wrong_actor_stale_requests_and_forged_runtime_binding_are_rejected() {
    let f = Fixture::new();
    let definition = supabricks_core::resource::ProjectId::new().to_string();
    let a = f.source("a", &definition);
    let b = f.source("b", &definition);
    let one = f.create(&a, "request");
    assert!(
        f.client(&a)
            .project(Command::Create {
                key: "request".into(),
                target: Some("staging".into())
            })
            .is_err()
    );
    assert!(
        serde_json::from_value::<Command>(json!({"action":"create","key":"k","actor_id":"admin"}))
            .is_err()
    );
    assert!(
        request(
            &f.root,
            Request::Api {
                api_version: 1,
                binding: Binding {
                    project_id: runtime(&one),
                    worktree: b
                },
                action: Action::Capabilities
            }
        )
        .is_err()
    );
    let doc = a.join("supabricks.toml");
    let old = fs::read_to_string(&doc).unwrap();
    fs::write(&doc, old.replace("version='0.1.0'", "version='0.1.1'")).unwrap();
    assert!(
        f.client(&a)
            .project(Command::Create {
                key: "request".into(),
                target: None
            })
            .is_err()
    );
}
#[test]
fn mcp_keeps_definition_identity_while_runtime_calls_use_deployment_identity() {
    let f = Fixture::new();
    let path = f.source(
        "mcp",
        &supabricks_core::resource::ProjectId::new().to_string(),
    );
    let client = f.client(&path);
    let mut session = supabricks_local::mcp::Session::default();
    session.dispatch(&client,json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"test","version":"1"},"capabilities":{}}}));
    session.dispatch(
        &client,
        json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
    );
    let call = |s: &mut supabricks_local::mcp::Session, name: &str, args: Value| {
        s.dispatch(&client,json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}})).unwrap()
    };
    assert_eq!(
        call(&mut session, "capabilities", json!({}))["result"]["isError"],
        true
    );
    let created = call(&mut session, "project_create", json!({"key":"mcp"}));
    assert_eq!(created["result"]["isError"], false, "{created}");
    let context = created["result"]["structuredContent"].clone();
    let caps = call(&mut session, "capabilities", json!({}));
    assert_eq!(
        caps["result"]["structuredContent"]["project_id"],
        context["runtime_project_id"]
    );
    let inspected = call(&mut session, "project_inspect", json!({}));
    assert_eq!(
        inspected["result"]["structuredContent"]["definition"]["id"],
        context["definition_id"]
    );
    assert_eq!(
        call(
            &mut session,
            "project_attach",
            json!({"deployment":context["deployment_id"],"worktree":"/tmp/elsewhere"})
        )["error"]["code"],
        -32602
    );
}
#[test]
fn cli_fork_creates_a_new_unbound_definition_without_changing_source() {
    let f = Fixture::new();
    let path = f.source(
        "source",
        &supabricks_core::resource::ProjectId::new().to_string(),
    );
    let original = fs::read(path.join("supabricks.toml")).unwrap();
    let dest = f.temp.path().join("fork");
    let result = Process::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["project", "fork", "--project"])
        .arg(&path)
        .arg("--destination")
        .arg(&dest)
        .args(["--name", "forked"])
        .env_remove("HOME")
        .env("PATH", "/no-tools")
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let fork: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_ne!(fork["definition_id"], fork["origin_definition_id"]);
    assert!(Client::bind(&f.root, &dest).is_err());
    assert_eq!(fs::read(path.join("supabricks.toml")).unwrap(), original);
}

#[test]
fn stopped_restore_preserves_destination_identity_binding_and_selection() {
    let mut f = Fixture::new();
    let path = f.source(
        "restore",
        &supabricks_core::resource::ProjectId::new().to_string(),
    );
    let context = f.create(&path, "create");
    let client = Client::bind(&f.root, &path).unwrap();
    let branch = database(&client, "main")["branch_id"]
        .as_str()
        .unwrap()
        .to_owned();
    client
        .call(Action::SelectBranch {
            branch: branch.clone(),
        })
        .unwrap();
    let mut daemon = f.daemon.take().unwrap();
    daemon.kill().unwrap();
    daemon.wait().unwrap();
    let backup = f.temp.path().join("backup");
    supabricks_local::recovery::create(&f.root, &backup).unwrap();
    let restored = f.temp.path().join("restored");
    supabricks_local::recovery::restore(&backup, &restored).unwrap();
    f.root = restored;
    f.start();
    let client = Client::bind(&f.root, &path).unwrap();
    assert_eq!(client.project(Command::Inspect).unwrap(), context);
    assert_eq!(client.call(Action::Selection).unwrap()["branch_id"], branch);
}
