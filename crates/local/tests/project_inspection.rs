use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
    process::Command,
};
use supabricks_local::{
    api::{Binding, ProjectSourceCommand},
    client::Client,
    mcp::Session,
    project::ProjectConfig,
    projects,
    store::Store,
};
fn copy(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let dest = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy(&entry.path(), &dest);
        } else {
            fs::copy(entry.path(), dest).unwrap();
        }
    }
}
fn fixture() -> tempfile::TempDir {
    let t = tempfile::tempdir().unwrap();
    copy(
        &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../examples/projects/sales"),
        t.path(),
    );
    t
}
fn edit(root: &Path, old: &str, new: &str) {
    let path = root.join("supabricks.toml");
    let text = fs::read_to_string(&path).unwrap();
    assert!(text.contains(old));
    fs::write(path, text.replace(old, new)).unwrap();
}
fn inspect(root: &Path) -> Value {
    serde_json::to_value(projects::inspect(root, None).unwrap()).unwrap()
}
#[test]
fn canonical_graph_is_portable_and_matches_checked_contract() {
    let a = fixture();
    let b = fixture();
    let report = inspect(a.path());
    assert_eq!(report, inspect(b.path()));
    assert_eq!(
        report["order"],
        json!(["database.main", "query.sales_total", "notebook.sales"])
    );
    assert_eq!(
        report["capabilities"],
        json!(["managed-notebooks", "postgres17", "spark-sql"])
    );
    assert_eq!(
        report["unresolved_bindings"],
        json!([{"resource":"database.main","kind":"destination_database"}])
    );
    assert_eq!(report["execution_supported"], false);
    assert_eq!(report["files"].as_object().unwrap().len(), 7);
    assert!(!report.to_string().contains(a.path().to_str().unwrap()));
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/project-inspection.json");
    let current = serde_json::to_string_pretty(&report).unwrap() + "\n";
    if std::env::var_os("UPDATE_LOCAL_SNAPSHOTS").is_some() {
        fs::write(&path, &current).unwrap();
    }
    assert_eq!(current, fs::read_to_string(path).unwrap());
}
#[test]
fn legacy_projects_keep_identity_and_runtime_binding_without_writing() {
    let t = tempfile::tempdir().unwrap();
    let config = ProjectConfig::initialize(t.path(), "legacy").unwrap();
    let before = fs::read(t.path().join("supabricks.toml")).unwrap();
    let report = inspect(t.path());
    assert_eq!(report["definition"]["id"], config.id.to_string());
    assert_eq!(report["execution_supported"], true);
    assert_eq!(report["files"].as_object().unwrap().len(), 1);
    assert!(Client::bind(&t.path().join("absent"), t.path()).is_ok());
    assert_eq!(before, fs::read(t.path().join("supabricks.toml")).unwrap());
    assert!(!t.path().join("absent").exists());
}
#[test]
fn cli_inspection_needs_neither_home_daemon_nor_toolchain() {
    let t = fixture();
    let state = t.path().join("must-not-exist");
    let before = fs::read(t.path().join("notebooks/sales.ipynb")).unwrap();
    for command in ["inspect", "validate"] {
        let output = Command::new(env!("CARGO_BIN_EXE_supabricks"))
            .args(["project", command, "--project"])
            .arg(t.path())
            .arg("--data-dir")
            .arg(&state)
            .env_remove("HOME")
            .env("PATH", "/no/tools")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            serde_json::from_slice::<Value>(&output.stdout).unwrap(),
            inspect(t.path())
        );
    }
    assert!(!state.exists());
    assert_eq!(
        before,
        fs::read(t.path().join("notebooks/sales.ipynb")).unwrap()
    );
    let bad = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["project", "inspect", "--project"])
        .arg(t.path())
        .args(["--unknown", "x"])
        .output()
        .unwrap();
    assert_eq!(bad.status.code(), Some(2));
}
#[test]
fn preview_identity_cannot_register_runtime_resources() {
    let t = fixture();
    assert!(
        ProjectConfig::read(t.path())
            .unwrap_err()
            .to_string()
            .contains("inspection-only")
    );
    assert!(Client::bind(t.path(), t.path()).is_err());
    let client = Client::bind_source(t.path(), t.path()).unwrap();
    let state = tempfile::tempdir().unwrap();
    let mut store = Store::open(&state.path().join("state")).unwrap();
    let binding = Binding {
        project_id: client.binding.project_id,
        worktree: t.path().to_owned(),
    };
    assert!(binding.validate(&mut store).is_err());
}
fn mcp(client: &Client) -> Session {
    let mut s = Session::default();
    s.dispatch(client,json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}));
    s.dispatch(
        client,
        json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
    );
    s
}
#[test]
fn mcp_source_tools_are_offline_strict_and_identity_bound() {
    let t = fixture();
    let client = Client::bind_source(&t.path().join("absent"), t.path()).unwrap();
    let mut s = mcp(&client);
    for tool in ["project_inspect", "project_validate"] {
        let r=s.dispatch(&client,json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":tool,"arguments":{}}})).unwrap();
        assert_eq!(r["result"]["structuredContent"], inspect(t.path()));
        assert_eq!(r["result"]["isError"], false);
    }
    let r=s.dispatch(&client,json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"project_inspect","arguments":{"path":"/another/project"}}})).unwrap();
    assert_eq!(r["error"]["code"], -32602);
    edit(
        t.path(),
        "707f5d76-71f2-4997-9bc0-71e375856031",
        "707f5d76-71f2-4997-9bc0-71e375856032",
    );
    assert!(
        client
            .inspect_source(ProjectSourceCommand::ProjectInspect { target: None })
            .unwrap_err()
            .to_string()
            .contains("identity changed")
    );
    assert!(!t.path().join("absent").exists());
}
#[test]
fn target_selection_is_explicit_and_never_a_permission() {
    let t = fixture();
    let a = inspect(t.path());
    let b = serde_json::to_value(projects::inspect(t.path(), Some("staging")).unwrap()).unwrap();
    assert_eq!(b["target"], "staging");
    assert_eq!(a["source_sha256"], b["source_sha256"]);
    assert_eq!(b["execution_supported"], false);
    assert!(projects::inspect(t.path(), Some("missing")).is_err());
    edit(t.path(), "default = true", "default = false");
    assert!(projects::inspect(t.path(), None).is_err());
    assert!(projects::inspect(t.path(), Some("local")).is_ok());
    edit(
        t.path(),
        "mode = \"production\"",
        "mode = \"production\"\ndefault = true",
    );
    edit(t.path(), "default = false", "default = true");
    assert!(projects::inspect(t.path(), Some("local")).is_err());
}
#[test]
fn rejects_unknown_security_hooks_interpolation_and_resource_kinds() {
    for (old, new) in [
        ("name = \"sales\"", "name = \"sales\"\nrun_as = \"admin\""),
        (
            "version = \"0.1.0\"",
            "version = \"0.1.0\"\nhook = \"touch /tmp/executed\"",
        ),
        ("kind = \"sql\"", "kind = \"job\""),
        (
            "\"postgres17\", \"spark-sql\"",
            "\"unity-catalog\", \"spark-sql\"",
        ),
        (
            "file = \"queries/sales_total.sql\"",
            "file = \"${HOME}/secret.sql\"",
        ),
        ("engine = \"postgres\"", "engine = \"shell\""),
        ("format_version = 2", "format_version = 99"),
    ] {
        let t = fixture();
        edit(t.path(), old, new);
        assert!(projects::inspect(t.path(), None).is_err(), "{new}");
    }
}
#[test]
fn graph_rejects_dangling_references_duplicates_and_cycles() {
    let t = fixture();
    edit(
        t.path(),
        "database = \"database.main\"",
        "database = \"database.missing\"",
    );
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    edit(
        t.path(),
        "environment = \"notebook\"",
        "environment = \"missing\"",
    );
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    fs::write(t.path().join("resources/database.toml"),"[resources.database.main]\nkind='postgres_database'\nlifecycle='retain'\ndepends_on=['notebook.sales']\n").unwrap();
    assert!(
        projects::inspect(t.path(), None)
            .unwrap_err()
            .to_string()
            .contains("cycle")
    );
    let t = fixture();
    edit(
        t.path(),
        "depends_on = [\"query.sales_total\"]",
        "depends_on = [\"query.missing\"]",
    );
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    let mut m = fs::read_to_string(t.path().join("supabricks.toml")).unwrap();
    m.push_str("\n[resources.database.main]\nkind='postgres_database'\nlifecycle='retain'\n");
    fs::write(t.path().join("supabricks.toml"), m).unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    fs::write(
        t.path().join("resources/database.toml"),
        "include=['resources/database.toml']\n",
    )
    .unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
}
#[test]
fn sources_require_declared_locks_valid_notebooks_and_inventory_selection() {
    let t = fixture();
    fs::remove_file(t.path().join("notebooks/environment/uv.lock")).unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    fs::write(
        t.path().join("notebooks/environment/uv.lock"),
        "version=9\npackage=[]\n",
    )
    .unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    fs::write(t.path().join("notebooks/sales.ipynb"), "{}").unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    edit(t.path(), "\"queries/*.sql\", ", "");
    assert!(projects::inspect(t.path(), None).is_err());
}
#[test]
fn path_traversal_private_inputs_symlinks_and_hardlinks_are_rejected() {
    for path in [
        "../secret",
        "/etc/passwd",
        "queries/../secret",
        ".env",
        "a\\b",
        "secret.KEY",
    ] {
        let t = fixture();
        edit(t.path(), "queries/*.sql", path);
        assert!(projects::inspect(t.path(), None).is_err(), "{path}");
    }
    let outside = tempfile::tempdir().unwrap();
    fs::write(outside.path().join("secret.sql"), "secret").unwrap();
    let t = fixture();
    fs::remove_file(t.path().join("queries/sales_total.sql")).unwrap();
    symlink(
        outside.path().join("secret.sql"),
        t.path().join("queries/sales_total.sql"),
    )
    .unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    fs::remove_dir_all(t.path().join("queries")).unwrap();
    symlink(outside.path(), t.path().join("queries")).unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
    let t = fixture();
    fs::hard_link(
        t.path().join("queries/sales_total.sql"),
        t.path().join("queries/link.sql"),
    )
    .unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
}
#[test]
fn oversized_files_fail_without_reading_or_running_them() {
    let t = fixture();
    fs::OpenOptions::new()
        .write(true)
        .open(t.path().join("queries/sales_total.sql"))
        .unwrap()
        .set_len(8 * 1024 * 1024 + 1)
        .unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
}
#[test]
fn recursive_globs_include_zero_and_many_directories() {
    let t = fixture();
    fs::create_dir_all(t.path().join("notebooks/nested/deeper")).unwrap();
    fs::copy(
        t.path().join("notebooks/sales.ipynb"),
        t.path().join("notebooks/nested/deeper/second.ipynb"),
    )
    .unwrap();
    let r = inspect(t.path());
    assert!(r["files"].get("notebooks/sales.ipynb").is_some());
    assert!(
        r["files"]
            .get("notebooks/nested/deeper/second.ipynb")
            .is_some()
    );
}

#[test]
fn glob_does_not_silently_omit_non_utf8_names() {
    use std::os::unix::ffi::OsStringExt;
    let t = fixture();
    let bad = std::ffi::OsString::from_vec(b"bad-\xff.sql".to_vec());
    fs::write(t.path().join("queries").join(bad), "SELECT 1").unwrap();
    assert!(projects::inspect(t.path(), None).is_err());
}
#[test]
fn modifying_selected_file_changes_inventory_digest() {
    let t = fixture();
    let original = inspect(t.path());
    fs::write(t.path().join("queries/sales_total.sql"), "SELECT 2;").unwrap();
    let changed = inspect(t.path());
    assert_ne!(original["source_sha256"], changed["source_sha256"]);
    assert_ne!(
        original["files"]["queries/sales_total.sql"],
        changed["files"]["queries/sales_total.sql"]
    );
}
