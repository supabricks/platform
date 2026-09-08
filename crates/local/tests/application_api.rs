use serde_json::{Value, json};
use std::{
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_local::{
    api::Action,
    client::{Client, diagnostic, request},
    daemon::Request,
    mcp::Session,
    project::ProjectConfig,
};
struct Daemon(Child);
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn start(root: &Path) -> Daemon {
    let d = Daemon(
        Command::new(env!("CARGO_BIN_EXE_supabricks"))
            .args(["daemon", "--data-dir"])
            .arg(root)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    while request(root, Request::Status).is_err() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    d
}
fn action(v: Value) -> Action {
    serde_json::from_value(v).unwrap()
}
#[test]
fn accepted_control_connection_waits_for_delayed_fragmented_request() {
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::net::UnixStream;
    let temp = tempfile::Builder::new()
        .prefix("sb-framing-")
        .tempdir_in("/tmp")
        .unwrap();
    let root = temp.path().join("state");
    let _daemon = start(&root);
    let mut stream = UnixStream::connect(root.join("control.sock")).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    // A connected client need not have a complete request queued at accept.
    // macOS used to inherit nonblocking mode and reply with EAGAIN here.
    std::thread::sleep(Duration::from_millis(150));
    stream.write_all(b"{\"version\":1,\"request\":").unwrap();
    std::thread::sleep(Duration::from_millis(150));
    stream.write_all(b"{\"method\":\"status\"}}\n").unwrap();
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).unwrap();
    let response: Value = serde_json::from_str(&line).unwrap();
    assert!(response.get("error").is_none(), "{response}");
    assert_eq!(response["result"]["engine_execution"], false);
}
#[test]
fn public_contract_replays_allocations_scopes_operations_and_fixes_worktree_binding() {
    let temp = tempfile::Builder::new()
        .prefix("sb-api-")
        .tempdir_in("/tmp")
        .unwrap();
    let root = temp.path().join("state");
    let one = temp.path().join("one");
    let two = temp.path().join("two");
    let foreign = temp.path().join("foreign");
    for p in [&one, &two, &foreign] {
        std::fs::create_dir(p).unwrap();
    }
    ProjectConfig::initialize(&one, "orders").unwrap();
    std::fs::copy(one.join("supabricks.toml"), two.join("supabricks.toml")).unwrap();
    ProjectConfig::initialize(&foreign, "other").unwrap();
    let mut d = start(&root);
    let a = Client::bind(&root, &one).unwrap();
    let b = Client::bind(&root, &two).unwrap();
    let other = Client::bind(&root, &foreign).unwrap();
    let create = json!({"action":"create_database","name":"main","key":"create-main"});
    let op = a.call(action(create.clone())).unwrap();
    assert_eq!(op["status"], "pending");
    assert_eq!(op["steps"], json!(["ensure_timeline", "start_compute"]));
    assert_eq!(a.call(action(create.clone())).unwrap(), op);
    let error = a
        .call(action(
            json!({"action":"create_database","name":"different","key":"create-main"}),
        ))
        .unwrap_err();
    assert_eq!(diagnostic(&error)["exit_code"], 4);
    let branch = op["branch_id"].as_str().unwrap().to_owned();
    a.call(Action::SelectBranch {
        branch: branch.clone(),
    })
    .unwrap();
    assert_eq!(a.call(Action::Selection).unwrap()["branch_id"], branch);
    assert_eq!(
        diagnostic(&b.call(Action::Selection).unwrap_err())["exit_code"],
        3
    );
    assert!(
        other
            .call(Action::GetOperation {
                id: serde_json::from_value(op["id"].clone()).unwrap()
            })
            .is_err()
    );
    assert!(
        other
            .call(Action::GetBranch {
                branch: branch.clone()
            })
            .is_err()
    );
    assert!(a.call(action(json!({"action":"set_state","branch":branch,"expected_revision":99,"desired":"running","key":"stale"}))).is_err());
    assert!(a.call(action(json!({"action":"delete_branch","branch":branch,"expected_revision":1,"key":"protected"}))).is_err());
    assert!(
        a.call(action(
            json!({"action":"sql","sql":"SELECT 1","read_only":false})
        ))
        .is_err()
    );
    assert!(
        request(
            &root,
            Request::Api {
                api_version: 99,
                binding: a.binding.clone(),
                action: Action::Capabilities
            }
        )
        .is_err()
    );
    let rows = a
        .call(Action::ListBranches {
            include_deleted: true,
        })
        .unwrap();
    let ports = &rows["branches"][0]["ports"];
    assert_ne!(ports["sql"], ports["external_http"]);
    d.0.kill().unwrap();
    d.0.wait().unwrap();
    drop(d);
    let _d = start(&root);
    assert_eq!(a.call(action(create)).unwrap(), op);
    assert_eq!(a.call(Action::Selection).unwrap()["branch_id"], branch);
    for n in 1..32 {
        a.call(Action::CreateDatabase {
            name: format!("root-{n}"),
            key: format!("root-{n}"),
        })
        .unwrap();
    }
    assert_eq!(
        diagnostic(
            &a.call(Action::CreateDatabase {
                name: "overflow".into(),
                key: "overflow".into()
            })
            .unwrap_err()
        )["code"],
        "conflict"
    );
    assert_eq!(
        a.call(Action::CreateDatabase {
            name: "main".into(),
            key: "create-main".into()
        })
        .unwrap(),
        op,
        "retries remain valid at quota"
    );
    std::fs::copy(foreign.join("supabricks.toml"), one.join("supabricks.toml")).unwrap();
    assert!(
        a.call(Action::Capabilities).is_err(),
        "long-lived sessions must reject project replacement"
    );
}
#[test]
fn mcp_contract_is_separate_strict_and_negotiated() {
    let temp = tempfile::tempdir().unwrap();
    ProjectConfig::initialize(temp.path(), "mcp").unwrap();
    let client = Client::bind(temp.path(), temp.path()).unwrap();
    let mut s = Session::default();
    assert_eq!(
        s.dispatch(
            &client,
            json!({"jsonrpc":"2.0","id":1,"method":"tools/list"})
        )
        .unwrap()["error"]["code"],
        -32600
    );
    let init=s.dispatch(&client,json!({"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"contract","version":"1"}}})).unwrap();
    assert_eq!(init["result"]["protocolVersion"], "2025-06-18");
    assert!(
        s.dispatch(
            &client,
            json!({"jsonrpc":"2.0","method":"notifications/initialized"})
        )
        .is_none()
    );
    let list = s
        .dispatch(
            &client,
            json!({"jsonrpc":"2.0","id":3,"method":"tools/list"}),
        )
        .unwrap();
    assert_eq!(list["result"]["tools"].as_array().unwrap().len(), 34);
    for args in [
        json!({"branch":"main","project_id":"other"}),
        json!({"action":"delete_branch"}),
    ] {
        let error=s.dispatch(&client,json!({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_branch","arguments":args}})).unwrap();
        assert_eq!(error["error"]["code"], -32602);
    }
    let error=s.dispatch(&client,json!({"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"capabilities","arguments":{}}})).unwrap();
    assert_eq!(error["result"]["isError"], true);
    assert_eq!(
        error["result"]["structuredContent"]["error"]["code"],
        "unavailable"
    );
    assert!(
        serde_json::from_value::<Action>(
            json!({"action":"set_ttl","branch":"main","expected_revision":1,"key":"missing-expiry"})
        )
        .is_err()
    );
    let current = serde_json::to_string_pretty(&supabricks_local::mcp::tools()).unwrap();
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/local-mcp-tools.json"
    );
    if std::env::var_os("UPDATE_LOCAL_SNAPSHOTS").is_some() {
        std::fs::write(path, &current).unwrap();
    }
    assert_eq!(current, std::fs::read_to_string(path).unwrap());
}
#[test]
fn cli_input_errors_are_machine_readable_and_do_not_create_state() {
    let temp = tempfile::tempdir().unwrap();
    for args in [
        vec!["nonsense"],
        vec!["up", "--bundle", "missing"],
        vec!["init", "orders", "--unknown", "value"],
        vec!["mcp"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_supabricks"))
            .args(args)
            .arg("--data-dir")
            .arg(temp.path().join("state"))
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let error: Value = serde_json::from_slice(&output.stderr).unwrap();
        assert_eq!(error["error"]["code"], "invalid_input");
    }
    assert!(!temp.path().join("state").exists());
}

#[test]
fn down_waits_for_ownership_release_after_the_socket_disappears() {
    use std::{
        io::{BufRead, BufReader, Write},
        os::unix::net::UnixListener,
    };
    let temp = tempfile::Builder::new()
        .prefix("sb-stop-")
        .tempdir_in("/tmp")
        .unwrap();
    let root = temp.path().join("state");
    let server_root = root.clone();
    let (ready, started) = std::sync::mpsc::channel();
    let (unlinked, missing_socket) = std::sync::mpsc::channel();
    let server = std::thread::spawn(move || {
        let store = supabricks_local::store::Store::open(&server_root).unwrap();
        let socket = server_root.join("control.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        ready.send(()).unwrap();
        let (mut stream, _) = listener.accept().unwrap();
        let mut line = String::new();
        BufReader::new(stream.try_clone().unwrap())
            .read_line(&mut line)
            .unwrap();
        let request: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(request["request"]["method"], "shutdown");
        writeln!(
            stream,
            "{}",
            json!({"version":1,"result":{"stopping":true}})
        )
        .unwrap();
        std::fs::remove_file(socket).unwrap();
        unlinked.send(()).unwrap();
        std::thread::sleep(Duration::from_millis(400));
        drop(store);
    });
    started.recv_timeout(Duration::from_secs(5)).unwrap();
    let first = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["down", "--data-dir"])
        .arg(&root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    missing_socket.recv_timeout(Duration::from_secs(5)).unwrap();
    let second = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["down", "--data-dir"])
        .arg(&root)
        .output()
        .unwrap();
    let output = first.wait_with_output().unwrap();
    server.join().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["status"], "stopped");
    assert!(
        second.status.success(),
        "{}",
        String::from_utf8_lossy(&second.stderr)
    );
}
