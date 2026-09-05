use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_core::resource::ProjectId;
use supabricks_local::{
    daemon::{Envelope, Request},
    operations::{Mutation, Ports},
    project::ProjectConfig,
};
struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn spawn(root: &Path) -> ChildGuard {
    ChildGuard(
        Command::new(env!("CARGO_BIN_EXE_supabricks"))
            .args(["daemon", "--data-dir"])
            .arg(root)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    )
}
fn call(root: &Path, request: Request) -> Value {
    let mut stream = UnixStream::connect(root.join("control.sock")).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    writeln!(
        stream,
        "{}",
        serde_json::to_string(&Envelope {
            version: 1,
            request
        })
        .unwrap()
    )
    .unwrap();
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).unwrap();
    serde_json::from_str(&line).unwrap()
}
fn ready(root: &Path) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !root.join("control.sock").exists() {
        assert!(Instant::now() < deadline, "daemon startup timed out");
        std::thread::sleep(Duration::from_millis(10));
    }
}
#[test]
fn simultaneous_startup_has_one_owner_and_kill_restart_retains_intent() {
    use std::os::unix::fs::PermissionsExt;
    let root = tempfile::Builder::new()
        .prefix("sb-daemon-")
        .tempdir_in("/tmp")
        .unwrap();
    let data = root.path().join("state");
    let mut a = spawn(&data);
    let mut b = spawn(&data);
    ready(&data);
    let deadline = Instant::now() + Duration::from_secs(10);
    let winner = loop {
        let sa = a.0.try_wait().unwrap();
        let sb = b.0.try_wait().unwrap();
        match (sa, sb) {
            (Some(exit), None) => {
                assert!(!exit.success());
                break &mut b;
            }
            (None, Some(exit)) => {
                assert!(!exit.success());
                break &mut a;
            }
            (Some(_), Some(_)) => panic!("both daemons exited"),
            _ => {
                assert!(Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    };
    let status = call(&data, Request::Status);
    let generation = status["result"]["generation"].as_i64().unwrap();
    assert_eq!(status["result"]["engine_execution"], false);
    let project = ProjectConfig {
        format_version: 1,
        id: ProjectId::new(),
        name: "daemon".into(),
    };
    assert!(
        call(
            &data,
            Request::RegisterProject {
                config: project.clone()
            }
        )
        .get("error")
        .is_none()
    );
    // Simultaneous retries enter the same single writer and reserve one resource.
    let calls: Vec<_> = (0..8)
        .map(|_| {
            let data = data.clone();
            let project = project.clone();
            std::thread::spawn(move || {
                call(
                    &data,
                    Request::Submit {
                        project_id: project.id,
                        key: "same".into(),
                        mutation: Mutation::CreateBranch {
                            name: "main".into(),
                            parent_id: None,
                            ports: Ports {
                                sql: 5400,
                                external_http: 5401,
                                internal_http: 5402,
                            },
                        },
                    },
                )
            })
        })
        .collect();
    let results: Vec<_> = calls.into_iter().map(|t| t.join().unwrap()).collect();
    for result in &results {
        assert_eq!(result["result"]["id"], results[0]["result"]["id"]);
        assert!(result.get("error").is_none());
    }
    assert_eq!(
        call(&data, Request::Pending)["result"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    for file in [
        "owner.lock",
        "state.sqlite3",
        "state.sqlite3-wal",
        "state.sqlite3-shm",
        "control.sock",
    ] {
        assert_eq!(
            fs::metadata(data.join(file)).unwrap().permissions().mode() & 0o077,
            0,
            "{file}"
        );
    }
    // A malformed client does not take down the daemon or leak payloads.
    let mut stream = UnixStream::connect(data.join("control.sock")).unwrap();
    stream.write_all(b"not-json-password-secret\n").unwrap();
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).unwrap();
    assert!(!line.contains("password-secret"));
    winner.0.kill().unwrap();
    winner.0.wait().unwrap();
    assert!(data.join("control.sock").exists());
    let mut restarted = spawn(&data);
    let deadline = Instant::now() + Duration::from_secs(10);
    while UnixStream::connect(data.join("control.sock")).is_err() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        call(&data, Request::Status)["result"]["generation"]
            .as_i64()
            .unwrap()
            > generation
    );
    assert_eq!(
        call(&data, Request::Pending)["result"][0]["id"],
        results[0]["result"]["id"]
    );
    assert_eq!(
        call(&data, Request::Shutdown)["result"],
        json!({"stopping":true})
    );
    assert!(restarted.0.wait().unwrap().success());
    assert!(!data.join("control.sock").exists());
    assert!(data.join("owner.lock").exists());
}
