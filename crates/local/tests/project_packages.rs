use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_local::{
    api::Action,
    client::{Client, request},
    daemon::Request,
    project::ProjectConfig,
    projects::{self, package},
};
fn copy(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for item in fs::read_dir(from).unwrap() {
        let item = item.unwrap();
        let dest = to.join(item.file_name());
        if item.file_type().unwrap().is_dir() {
            copy(&item.path(), &dest);
        } else {
            fs::copy(item.path(), dest).unwrap();
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
#[test]
fn deterministic_transport_and_content_fixture_round_trip_across_roots() {
    let a = fixture();
    let b = fixture();
    let output = tempfile::tempdir().unwrap();
    let one = output.path().join("one.sbproj");
    let two = output.path().join("two.sbproj");
    fs::set_permissions(
        b.path().join("queries/sales_total.sql"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let report = package::pack(a.path(), &one, None).unwrap();
    package::pack(b.path(), &two, None).unwrap();
    assert_eq!(fs::read(&one).unwrap(), fs::read(&two).unwrap());
    let value = serde_json::to_value(&report).unwrap();
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/project-package.json");
    let expected = serde_json::to_string_pretty(&value).unwrap() + "\n";
    if std::env::var_os("UPDATE_LOCAL_SNAPSHOTS").is_some() {
        fs::write(&fixture, &expected).unwrap();
    }
    assert_eq!(expected, fs::read_to_string(fixture).unwrap());
    assert_eq!(
        serde_json::to_value(package::verify(&one, None).unwrap()).unwrap(),
        value
    );
    let destination = output.path().join("unpacked");
    package::unpack(&one, &destination, None).unwrap();
    assert_eq!(
        fs::metadata(&destination).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(destination.join("notebooks"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(destination.join("supabricks.toml"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let marker: Value =
        serde_json::from_slice(&fs::read(destination.join("supabricks-unpacked.json")).unwrap())
            .unwrap();
    assert_eq!(marker["state"], "unbound");
    assert!(ProjectConfig::read(&destination).is_err());
    assert_eq!(
        serde_json::to_value(projects::inspect(&destination, None).unwrap()).unwrap(),
        value["inspection"]
    );
    // The completion receipt is not re-imported as payload when repackaging.
    let again = output.path().join("again.sbproj");
    package::pack(&destination, &again, None).unwrap();
    assert_eq!(fs::read(&one).unwrap(), fs::read(again).unwrap());
}
#[test]
fn output_stripping_preserves_source_and_removes_widget_and_runtime_state() {
    let source = fixture();
    let output = tempfile::tempdir().unwrap();
    let path = source.path().join("notebooks/sales.ipynb");
    let mut doc: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    doc["metadata"]["widgets"] = json!({"private":"state"});
    doc["metadata"]["supabricks"] = json!({"local_endpoint":"private"});
    doc["cells"][0]["metadata"]["execution"] = json!({"started":"yesterday"});
    doc["cells"][1]["execution_count"] = json!(9);
    doc["cells"][1]["outputs"] =
        json!([{"output_type":"stream","name":"stdout","text":"private output"}]);
    fs::write(&path, serde_json::to_vec(&doc).unwrap()).unwrap();
    let before = fs::read(&path).unwrap();
    let archive = output.path().join("source.sbproj");
    package::pack(source.path(), &archive, None).unwrap();
    assert_eq!(fs::read(&path).unwrap(), before);
    let dest = output.path().join("copy");
    package::unpack(&archive, &dest, None).unwrap();
    let clean: Value =
        serde_json::from_slice(&fs::read(dest.join("notebooks/sales.ipynb")).unwrap()).unwrap();
    assert!(clean["metadata"].get("widgets").is_none());
    assert!(clean["metadata"].get("supabricks").is_none());
    assert!(clean["cells"][0]["metadata"].get("execution").is_none());
    assert_eq!(clean["cells"][1]["execution_count"], Value::Null);
    assert_eq!(clean["cells"][1]["outputs"], json!([]));
}
#[test]
fn private_inputs_and_credentials_never_publish_and_undeclared_files_are_absent() {
    let source = fixture();
    let output = tempfile::tempdir().unwrap();
    for file in [
        ".env",
        "connections.json",
        "runtime.json",
        "secret.pem",
        "state.sqlite3",
    ] {
        fs::write(source.path().join(file), "private").unwrap();
    }
    let archive = output.path().join("one.sbproj");
    let report = package::pack(source.path(), &archive, None).unwrap();
    assert_eq!(report.inspection.files.len(), 7);
    for name in [
        ".env",
        "connections.json",
        "runtime.json",
        "secret.pem",
        "state.sqlite3",
    ] {
        let s = fixture();
        fs::write(s.path().join(name), "private").unwrap();
        edit(s.path(), "fixtures/sales.csv", name);
        let dest = output.path().join(format!("{name}.sbproj"));
        assert!(package::pack(s.path(), &dest, None).is_err());
        assert!(!dest.exists());
    }
    let py = source.path().join("notebooks/environment/pyproject.toml");
    fs::write(
        &py,
        format!(
            "{}\n[[tool.uv.index]]\nurl = 'https://user:password@example.test/simple'\n",
            fs::read_to_string(&py).unwrap()
        ),
    )
    .unwrap();
    let dest = output.path().join("credentials.sbproj");
    assert!(package::pack(source.path(), &dest, None).is_err());
    assert!(!dest.exists());
}
#[test]
fn publication_never_overwrites_and_failure_never_exposes_a_partial_project() {
    let source = fixture();
    let output = tempfile::tempdir().unwrap();
    let archive = output.path().join("a.sbproj");
    package::pack(source.path(), &archive, None).unwrap();
    let original = fs::read(&archive).unwrap();
    assert!(package::pack(source.path(), &archive, None).is_err());
    assert_eq!(original, fs::read(&archive).unwrap());
    let dest = output.path().join("existing");
    fs::create_dir(&dest).unwrap();
    fs::write(dest.join("keep"), "keep").unwrap();
    assert!(package::unpack(&archive, &dest, None).is_err());
    assert_eq!(fs::read_to_string(dest.join("keep")).unwrap(), "keep");
    let link = output.path().join("link");
    symlink(&dest, &link).unwrap();
    assert!(package::unpack(&archive, &link, None).is_err());
    let corrupted = output.path().join("corrupt.sbproj");
    let mut bytes = original;
    bytes.truncate(bytes.len() / 2);
    fs::write(&corrupted, bytes).unwrap();
    let incomplete = output.path().join("incomplete");
    assert!(package::unpack(&corrupted, &incomplete, None).is_err());
    assert!(!incomplete.exists());
    assert!(!fs::read_dir(output.path()).unwrap().any(|e| {
        e.unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".supabricks-package-")
    }));
}
#[test]
fn all_archive_commands_work_without_home_tools_or_runtime_state() {
    let source = fixture();
    let output = tempfile::tempdir().unwrap();
    let archive = output.path().join("offline.sbproj");
    let dest = output.path().join("copy");
    let state = output.path().join("state");
    let run = |args: &[&str]| {
        let result = Command::new(env!("CARGO_BIN_EXE_supabricks"))
            .args(args)
            .args(["--data-dir", state.to_str().unwrap()])
            .env_remove("HOME")
            .env("PATH", "/no-tools")
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        serde_json::from_slice::<Value>(&result.stdout).unwrap()
    };
    let packed = run(&[
        "project",
        "pack",
        "--project",
        source.path().to_str().unwrap(),
        "--output",
        archive.to_str().unwrap(),
    ]);
    assert_eq!(
        packed,
        run(&["project", "inspect", archive.to_str().unwrap()])
    );
    assert_eq!(
        packed,
        run(&["project", "verify", archive.to_str().unwrap()])
    );
    assert_eq!(
        packed,
        run(&[
            "project",
            "unpack",
            archive.to_str().unwrap(),
            "--destination",
            dest.to_str().unwrap()
        ])
    );
    assert!(!state.exists());
    let staged = package::verify(&archive, Some("staging")).unwrap();
    assert_eq!(staged.inspection.target, "staging");
    assert_eq!(staged.content_sha256, packed["content_sha256"]);
    assert!(package::verify(&archive, Some("unknown")).is_err());
}
#[test]
fn format_one_is_not_silently_packaged_or_converted() {
    let source = tempfile::tempdir().unwrap();
    let output = tempfile::tempdir().unwrap();
    ProjectConfig::initialize(source.path(), "legacy").unwrap();
    let before = fs::read(source.path().join("supabricks.toml")).unwrap();
    assert!(package::pack(source.path(), &output.path().join("legacy.sbproj"), None).is_err());
    assert_eq!(
        before,
        fs::read(source.path().join("supabricks.toml")).unwrap()
    );
}
#[test]
fn explicit_saved_query_api_and_cli_export_are_revision_fenced_and_project_scoped() {
    struct Daemon(std::process::Child);
    impl Drop for Daemon {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let t = tempfile::Builder::new()
        .prefix("sb-pk02-")
        .tempdir_in("/tmp")
        .unwrap();
    let root = t.path().join("state");
    let first = t.path().join("first");
    let second = t.path().join("second");
    fs::create_dir(&first).unwrap();
    fs::create_dir(&second).unwrap();
    let config = ProjectConfig::initialize(&first, "first").unwrap();
    ProjectConfig::initialize(&second, "second").unwrap();
    let _daemon = Daemon(
        Command::new(env!("CARGO_BIN_EXE_supabricks"))
            .args(["daemon", "--data-dir"])
            .arg(&root)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    while request(&root, Request::Status).is_err() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    let id = supabricks_core::resource::OperationId::new();
    let directory = root.join("queries").join(config.id.to_string());
    fs::create_dir_all(&directory).unwrap();
    let path = directory.join(format!("{id}.json"));
    let bytes=serde_json::to_vec(&json!({"version":1,"id":id,"revision":3,"target":{"branch":supabricks_core::resource::BranchId::new(),"revision":1},"title":"Example","sql":"SELECT 'kept private until exported';"})).unwrap();
    fs::write(&path, &bytes).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let client = Client::bind(&root, &first).unwrap();
    let other = Client::bind(&root, &second).unwrap();
    let action = || Action::SavedQueryExport {
        id,
        expected_revision: 3,
    };
    let exported = client.call(action()).unwrap();
    assert!(exported.get("target").is_none());
    assert!(other.call(action()).is_err());
    assert!(
        client
            .call(Action::SavedQueryExport {
                id,
                expected_revision: 2
            })
            .is_err()
    );
    let output = t.path().join("export.sql");
    let result = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args([
            "project",
            "export-query",
            &id.to_string(),
            "--expected-revision",
            "3",
            "--output",
        ])
        .arg(&output)
        .arg("--project")
        .arg(&first)
        .arg("--data-dir")
        .arg(&root)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(fs::read_to_string(output).unwrap(), exported["sql"]);
    assert_eq!(fs::read(path).unwrap(), bytes);
}
