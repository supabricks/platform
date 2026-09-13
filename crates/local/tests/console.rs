use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_local::{
    api::Binding, client::request, console::assets::Assets, daemon::Request, project::ProjectConfig,
};

struct Fixture {
    temp: tempfile::TempDir,
    root: PathBuf,
    project: PathBuf,
    assets: PathBuf,
    daemon: Child,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::Builder::new()
            .prefix("sb-console-")
            .tempdir_in("/tmp")
            .unwrap();
        let base = temp.path().canonicalize().unwrap();
        let root = base.join("data");
        let project = base.join("app");
        fs::create_dir(&project).unwrap();
        ProjectConfig::initialize(&project, "console-test").unwrap();
        let assets = base.join("assets");
        fs::create_dir(&assets).unwrap();
        fs::write(
            assets.join("index.html"),
            "<!doctype html><title>Console</title>",
        )
        .unwrap();
        manifest(&assets, 1);
        let daemon = spawn(&root);
        wait(|| request(&root, Request::Status).is_ok());
        Self {
            temp,
            root,
            project,
            assets,
            daemon,
        }
    }
    fn binding(&self) -> Binding {
        Binding {
            project_id: ProjectConfig::read(&self.project).unwrap().id,
            worktree: self.project.clone(),
        }
    }
    fn open(&self) -> Value {
        let mut value = Value::Null;
        wait(|| {
            value = request(
                &self.root,
                Request::ConsoleOpen {
                    binding: self.binding(),
                    assets: self.assets.clone(),
                },
            )
            .unwrap();
            value["state"] == "ready"
        });
        value
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = request(&self.root, Request::Shutdown);
        let deadline = Instant::now() + Duration::from_secs(5);
        while self.daemon.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = self.daemon.kill();
        let _ = self.daemon.wait();
        // Recover owned orphan processes even when the test deliberately killed the daemon.
        let _ = Command::new(env!("CARGO_BIN_EXE_supabricks"))
            .args(["down", "--data-dir"])
            .arg(&self.root)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        let _ = self.temp.path();
    }
}
fn spawn(root: &Path) -> Child {
    Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["daemon", "--data-dir"])
        .arg(root)
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap()
}
fn wait(mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(12);
    while !f() {
        assert!(Instant::now() < deadline, "console condition timed out");
        std::thread::sleep(Duration::from_millis(40));
    }
}
fn manifest(root: &Path, version: u32) {
    fs::write(
        root.join("console.json"),
        json!({"api_version":version,"files":{
        "index.html":hex::encode(Sha256::digest(fs::read(root.join("index.html")).unwrap()))}})
        .to_string(),
    )
    .unwrap();
}
struct Http {
    status: u16,
    headers: String,
    body: Vec<u8>,
}
fn http(origin: &str, method: &str, path: &str, headers: &[(&str, &str)], body: &str) -> Http {
    let host = origin.strip_prefix("http://").unwrap();
    let mut stream = TcpStream::connect(host).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    write!(
        stream,
        "{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    )
    .unwrap();
    for (name, value) in headers {
        write!(stream, "{name}: {value}\r\n").unwrap();
    }
    write!(stream, "\r\n{body}").unwrap();
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).unwrap();
    let end = bytes.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    let headers = String::from_utf8(bytes[..end].to_vec()).unwrap();
    let status = headers.split_whitespace().nth(1).unwrap().parse().unwrap();
    Http {
        status,
        headers,
        body: bytes[end + 4..].to_vec(),
    }
}
fn parts(value: &Value) -> (String, String) {
    let (origin, token) = value["url"]
        .as_str()
        .unwrap()
        .split_once("/#launch=")
        .unwrap();
    (origin.into(), token.into())
}
fn login(origin: &str, token: &str) -> Http {
    http(
        origin,
        "POST",
        "/api/session",
        &[
            ("Origin", origin),
            ("X-Supabricks-Console", "1"),
            ("Content-Type", "application/json"),
        ],
        &json!({"token":token}).to_string(),
    )
}

#[test]
fn notebook_control_waits_through_a_busy_daemon_without_resending() {
    let fixture = Fixture::new();
    let (origin, token) = parts(&fixture.open());
    let session = login(&origin, &token);
    assert_eq!(session.status, 200);
    let cookie = session
        .headers
        .lines()
        .find(|s| s.to_lowercase().starts_with("set-cookie:"))
        .unwrap()
        .split_once(':')
        .unwrap()
        .1
        .trim()
        .split(';')
        .next()
        .unwrap();
    let body: Value = serde_json::from_slice(&session.body).unwrap();

    // Simulate the admission work that exceeded the old two-second socket
    // deadline on macOS. Resume this fixture's exact child even if HTTP panics.
    let pid = fixture.daemon.id() as libc::pid_t;
    assert_eq!(unsafe { libc::kill(pid, libc::SIGSTOP) }, 0);
    let resume = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(3));
        assert_eq!(unsafe { libc::kill(pid, libc::SIGCONT) }, 0);
    });
    let response = http(
        &origin,
        "POST",
        "/api/workspace",
        &[
            ("Origin", &origin),
            ("Cookie", cookie),
            ("X-Supabricks-Console", "1"),
            ("X-Supabricks-CSRF", body["csrf"].as_str().unwrap()),
            ("Content-Type", "application/json"),
        ],
        &json!({"action":"notebook","command":{"action":"list"}}).to_string(),
    );
    resume.join().unwrap();
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&response.body).unwrap()["value"],
        json!([])
    );
}

#[test]
fn notebook_documents_cross_both_transports_with_string_revisions_and_conflicts() {
    let fixture = Fixture::new();
    let (origin, token) = parts(&fixture.open());
    let session = login(&origin, &token);
    let cookie = session
        .headers
        .lines()
        .find(|s| s.to_lowercase().starts_with("set-cookie:"))
        .unwrap()
        .split_once(':')
        .unwrap()
        .1
        .trim()
        .split(';')
        .next()
        .unwrap();
    let body: Value = serde_json::from_slice(&session.body).unwrap();
    let csrf = body["csrf"].as_str().unwrap();
    let headers = [
        ("Origin", origin.as_str()),
        ("Cookie", cookie),
        ("X-Supabricks-Console", "1"),
        ("X-Supabricks-CSRF", csrf),
        ("Content-Type", "application/json"),
    ];
    let call = |v: Value| {
        http(
            &origin,
            "POST",
            "/api/notebooks/contents",
            &headers,
            &v.to_string(),
        )
    };
    let cells:Vec<Value>=(0..6).map(|i|json!({"id":format!("cell{i}"),"cell_type":"code","metadata":{},"source":"#".repeat(400000),"outputs":[],"execution_count":null})).collect();
    let doc = json!({"cells":cells,"nbformat":4,"nbformat_minor":5,"metadata":{}});
    let saved = call(json!({"action":"save","path":"big.ipynb","document":doc}));
    assert_eq!(
        saved.status,
        200,
        "{}",
        String::from_utf8_lossy(&saved.body)
    );
    let revision =
        serde_json::from_slice::<Value>(&saved.body).unwrap()["value"]["revision"].clone();
    assert!(revision.is_string());
    let loaded = call(json!({"action":"get","path":"big.ipynb"}));
    assert_eq!(loaded.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&loaded.body).unwrap()["value"]["document"],
        doc
    );
    assert_eq!(
        call(
            json!({"action":"save","path":"big.ipynb","document":doc,"expected_revision":revision})
        )
        .status,
        200
    );
    assert_eq!(
        call(json!({"action":"save","path":"big.ipynb","document":doc})).status,
        409
    );
    assert_eq!(
        call(
            json!({"action":"save","path":"big.ipynb","document":doc,"expected_revision":"stale"})
        )
        .status,
        409
    );
    assert_eq!(call(json!({"action":"rename","path":"big.ipynb","destination":"renamed.ipynb","expected_revision":revision})).status,200);
}

#[test]
fn launch_is_single_use_and_browser_requests_are_scoped_and_bounded() {
    let fixture = Fixture::new();
    let (origin, token) = parts(&fixture.open());
    let index = http(&origin, "GET", "/", &[], "");
    assert_eq!(index.status, 200);
    assert!(
        index
            .headers
            .to_lowercase()
            .contains("content-security-policy: default-src 'none'")
    );
    assert!(!String::from_utf8(index.body).unwrap().contains(&token));
    assert_eq!(http(&origin, "GET", "/../config.json", &[], "").status, 404);
    assert_eq!(
        http(&origin, "GET", "/", &[("Host", "evil.example")], "").status,
        403
    );
    assert_eq!(
        http(
            &origin,
            "GET",
            "/api/overview",
            &[("X-Supabricks-Console", "1")],
            ""
        )
        .status,
        401
    );
    assert_eq!(
        http(
            &origin,
            "POST",
            "/api/session",
            &[
                ("Origin", "http://evil.example"),
                ("X-Supabricks-Console", "1")
            ],
            ""
        )
        .status,
        403
    );
    assert_eq!(
        http(
            &origin,
            "GET",
            "/api/overview",
            &[("X-Supabricks-Console", "2")],
            ""
        )
        .status,
        409
    );
    assert_eq!(
        http(
            &origin,
            "POST",
            "/api/session",
            &[
                ("Origin", &origin),
                ("X-Supabricks-Console", "1"),
                ("Content-Type", "application/json")
            ],
            &"x".repeat(4097)
        )
        .status,
        413
    );
    let session = login(&origin, &token);
    assert_eq!(session.status, 200);
    assert_eq!(login(&origin, &token).status, 401);
    let cookie = session
        .headers
        .lines()
        .find(|h| h.to_lowercase().starts_with("set-cookie:"))
        .unwrap()
        .split_once(": ")
        .unwrap()
        .1
        .split(';')
        .next()
        .unwrap();
    assert!(session.headers.contains("HttpOnly; SameSite=Strict"));
    let auth = [("Cookie", cookie), ("X-Supabricks-Console", "1")];
    // Repeated CLI launches in one browser must not exhaust the 32-session cap.
    for _ in 0..33 {
        let (_, ticket) = parts(&fixture.open());
        let renewal = http(
            &origin,
            "POST",
            "/api/session",
            &[
                ("Cookie", cookie),
                ("Origin", &origin),
                ("X-Supabricks-Console", "1"),
                ("Content-Type", "application/json"),
            ],
            &json!({"token":ticket}).to_string(),
        );
        assert_eq!(renewal.status, 200);
        let renewed: Value = serde_json::from_slice(&renewal.body).unwrap();
        let original: Value = serde_json::from_slice(&session.body).unwrap();
        assert_eq!(renewed["csrf"], original["csrf"]);
    }
    let overview = http(&origin, "GET", "/api/overview", &auth, "");
    assert_eq!(overview.status, 200);
    let overview: Value = serde_json::from_slice(&overview.body).unwrap();
    assert_eq!(overview["project"]["name"], "console-test");
    assert_eq!(overview["runtime"]["engine_enabled"], false);
    assert_eq!(overview["branches"], json!([]));
    let headers = [
        ("Cookie", cookie),
        ("X-Supabricks-Console", "1"),
        ("Origin", &origin),
    ];
    assert_eq!(
        http(&origin, "POST", "/api/logout", &headers, "").status,
        403
    );
    let csrf: Value = serde_json::from_slice(&session.body).unwrap();
    assert_eq!(
        http(
            &origin,
            "POST",
            "/api/logout",
            &[
                ("Cookie", cookie),
                ("X-Supabricks-Console", "1"),
                ("Origin", &origin),
                ("X-Supabricks-CSRF", csrf["csrf"].as_str().unwrap())
            ],
            ""
        )
        .status,
        200
    );
    assert_eq!(http(&origin, "GET", "/api/overview", &auth, "").status, 401);
    let (second, token) = parts(&fixture.open());
    assert_eq!(second, origin);
    assert_eq!(login(&second, &token).status, 200);
}

#[test]
fn restart_fences_console_and_rejects_old_generation_and_project_binding() {
    let mut fixture = Fixture::new();
    let (origin, token) = parts(&fixture.open());
    assert_eq!(login(&origin, &token).status, 200);
    let generation = request(&fixture.root, Request::Status).unwrap()["generation"]
        .as_i64()
        .unwrap();
    fixture.daemon.kill().unwrap();
    fixture.daemon.wait().unwrap();
    fixture.daemon = spawn(&fixture.root);
    wait(|| {
        request(&fixture.root, Request::Status)
            .is_ok_and(|v| v["generation"].as_i64().unwrap() > generation)
    });
    wait(|| TcpStream::connect(origin.strip_prefix("http://").unwrap()).is_err());
    assert!(
        request(
            &fixture.root,
            Request::ConsoleOverview {
                binding: fixture.binding(),
                generation
            }
        )
        .is_err()
    );
    let (origin, token) = parts(&fixture.open());
    assert_eq!(login(&origin, &token).status, 200);
    let old = fixture.binding();
    let mut config = ProjectConfig::read(&fixture.project).unwrap();
    config.id = supabricks_core::resource::ProjectId::new();
    fs::write(
        fixture.project.join("supabricks.toml"),
        toml::to_string(&config).unwrap(),
    )
    .unwrap();
    assert!(
        request(
            &fixture.root,
            Request::ConsoleOverview {
                binding: old,
                generation: generation + 1
            }
        )
        .is_err()
    );
    wait(|| TcpStream::connect(origin.strip_prefix("http://").unwrap()).is_err());
}

#[test]
fn shutdown_and_backup_account_for_console_without_retaining_authentication() {
    let fixture = Fixture::new();
    let (origin, _) = parts(&fixture.open());
    let backup = fixture.temp.path().join("backup");
    let output = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["backup", "create"])
        .arg(&backup)
        .arg("--data-dir")
        .arg(&fixture.root)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(TcpStream::connect(origin.strip_prefix("http://").unwrap()).is_err());
    assert!(!backup.join("data/tmp").exists());
    let manifest: Value =
        serde_json::from_slice(&fs::read(backup.join("backup.json")).unwrap()).unwrap();
    assert!(!manifest.to_string().contains("ticket.json"));
}

#[test]
fn assets_refuse_missing_mismatched_and_unsafe_payloads() {
    let f = Fixture::new();
    assert!(Assets::load(&f.assets).is_ok());
    manifest(&f.assets, 2);
    assert!(Assets::load(&f.assets).is_err());
    manifest(&f.assets, 1);
    fs::write(f.assets.join("index.html"), "changed").unwrap();
    assert!(Assets::load(&f.assets).is_err());
    manifest(&f.assets, 1);
    symlink(
        f.project.join("supabricks.toml"),
        f.assets.join("secret.html"),
    )
    .unwrap();
    assert!(Assets::load(&f.assets).is_err());
    assert!(Assets::load(&f.assets.join("missing")).is_err());
}

#[test]
fn independent_project_sessions_and_expired_links_are_refused() {
    let f = Fixture::new();
    let (origin, first) = parts(&f.open());
    let (_, second) = parts(&f.open());
    assert_eq!(login(&origin, &first).status, 200);
    let session = login(&origin, &second);
    assert_eq!(session.status, 200);
    let cookie = session
        .headers
        .lines()
        .find(|h| h.to_lowercase().starts_with("set-cookie:"))
        .unwrap()
        .split_once(": ")
        .unwrap()
        .1
        .split(';')
        .next()
        .unwrap();
    let other = f.temp.path().join("other-app");
    fs::create_dir(&other).unwrap();
    let config = ProjectConfig::initialize(&other, "other-project").unwrap();
    let binding = Binding {
        project_id: config.id,
        worktree: other.canonicalize().unwrap(),
    };
    let mut opened = Value::Null;
    wait(|| {
        opened = request(
            &f.root,
            Request::ConsoleOpen {
                binding: binding.clone(),
                assets: f.assets.clone(),
            },
        )
        .unwrap();
        opened["state"] == "ready"
    });
    let (other_origin, _) = parts(&opened);
    assert_ne!(other_origin, origin);
    assert_eq!(
        http(
            &other_origin,
            "GET",
            "/api/overview",
            &[("Cookie", cookie), ("X-Supabricks-Console", "1")],
            ""
        )
        .status,
        401
    );
    let (_, expired) = parts(&f.open());
    for entry in fs::read_dir(f.root.join("tmp")).unwrap() {
        let ticket = entry.unwrap().path().join(format!("ticket-{expired}.json"));
        if ticket.exists() {
            fs::write(
                ticket,
                json!({"token":expired,"expires_at_ms":0}).to_string(),
            )
            .unwrap();
        }
    }
    assert_eq!(login(&origin, &expired).status, 401);
}

#[test]
fn workspace_commands_enforce_csrf_revisions_private_saved_files_and_backup() {
    use std::os::unix::fs::MetadataExt;
    use supabricks_core::resource::OperationId;
    let f = Fixture::new();
    let (origin, token) = parts(&f.open());
    let auth = login(&origin, &token);
    let cookie = auth
        .headers
        .lines()
        .find_map(|l| {
            l.to_lowercase().starts_with("set-cookie:").then(|| {
                l.split_once(':')
                    .unwrap()
                    .1
                    .trim()
                    .split(';')
                    .next()
                    .unwrap()
                    .to_owned()
            })
        })
        .unwrap();
    let csrf = serde_json::from_slice::<Value>(&auth.body).unwrap()["csrf"]
        .as_str()
        .unwrap()
        .to_owned();
    let headers = [
        ("Origin", origin.as_str()),
        ("X-Supabricks-Console", "1"),
        ("Content-Type", "application/json"),
        ("Cookie", cookie.as_str()),
        ("X-Supabricks-CSRF", csrf.as_str()),
    ];
    let call = |value: Value| {
        http(
            &origin,
            "POST",
            "/api/workspace",
            &headers,
            &value.to_string(),
        )
    };
    assert_eq!(
        http(
            &origin,
            "POST",
            "/api/workspace",
            &headers[..4],
            "{\"action\":\"saved_list\"}"
        )
        .status,
        403
    );
    assert_eq!(
        call(json!({"action":"select_branch","branch":"main"})).status,
        400
    );
    let env =
        json!({"action":"environment","command":{"action":"find","key":"browser-package-test"}});
    assert_eq!(
        http(
            &origin,
            "POST",
            "/api/workspace",
            &headers[..4],
            &env.to_string()
        )
        .status,
        403
    );
    let inspected = call(env);
    assert_eq!(
        inspected.status,
        200,
        "{}",
        String::from_utf8_lossy(&inspected.body)
    );
    assert!(
        serde_json::from_slice::<Value>(&inspected.body).unwrap()["value"]["operation"].is_null()
    );
    assert_eq!(
        call(json!({"action":"environment","command":{"action":"inspect","project_id":"other"}}))
            .status,
        400
    );
    assert_eq!(call(json!({"action":"environment","command":{"action":"manage","key":"bad","change":{"kind":"add","requirement":"demo"}}})).status,400);
    let created = call(json!({"action":"create_database","name":"main","key":"saved-main"}));
    assert_eq!(
        created.status,
        200,
        "{}",
        String::from_utf8_lossy(&created.body)
    );
    let op: Value = serde_json::from_slice::<Value>(&created.body).unwrap()["value"].clone();
    let target = json!({"branch":op["branch_id"],"revision":op["revision"]});
    let id = OperationId::new();
    let saved = json!({"action":"saved_put","id":id,"expected_revision":0,"target":target,"title":"Private example","sql":"SELECT 9007199254740993::bigint"});
    assert_eq!(call(saved.clone()).status, 200);
    assert_eq!(call(saved).status, 409);
    let got = call(json!({"action":"saved_get","id":id}));
    assert_eq!(got.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&got.body).unwrap()["value"]["sql"],
        "SELECT 9007199254740993::bigint"
    );
    let relative = format!("queries/{}/{id}.json", f.binding().project_id);
    let path = f.root.join(&relative);
    assert_eq!(fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
    let mut wrong = target.clone();
    wrong["revision"] = json!(999);
    assert_eq!(call(json!({"action":"saved_put","id":id,"expected_revision":1,"target":wrong,"title":"bad","sql":"SELECT 1"})).status,409);
    assert_eq!(
        call(json!({"action":"saved_delete","id":id,"expected_revision":9})).status,
        409
    );
    // Traversal is rejected by UUID parsing, and symlinked saved records fail closed.
    assert_eq!(
        call(json!({"action":"saved_get","id":"../../runtime.json"})).status,
        400
    );
    let symlink_id = OperationId::new();
    let link = path.parent().unwrap().join(format!("{symlink_id}.json"));
    symlink(&path, &link).unwrap();
    assert_eq!(
        call(json!({"action":"saved_get","id":symlink_id})).status,
        400
    );
    fs::remove_file(link).unwrap();
    let generation = request(&f.root, Request::Status).unwrap()["generation"]
        .as_i64()
        .unwrap();
    assert!(
        request(
            &f.root,
            Request::ConsoleAction {
                binding: f.binding(),
                generation: generation - 1,
                owner: format!("{}:{}", OperationId::new(), "a".repeat(64)),
                action: serde_json::from_value(json!({"action":"saved_list"})).unwrap()
            }
        )
        .is_err()
    );
    let escaped_id = OperationId::new();
    let escaped_sql = format!("SELECT '{}'", "\\".repeat(22000));
    assert_eq!(call(json!({"action":"saved_put","id":escaped_id,"expected_revision":0,"target":target,"title":"Escaped SQL","sql":escaped_sql})).status,200);
    let escaped = call(json!({"action":"saved_get","id":escaped_id}));
    assert_eq!(escaped.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&escaped.body).unwrap()["value"]["sql"],
        escaped_sql
    );
    let other = f.temp.path().join("other-project");
    fs::create_dir(&other).unwrap();
    let other_config = ProjectConfig::initialize(&other, "other-project").unwrap();
    let other_binding = Binding {
        project_id: other_config.id,
        worktree: other,
    };
    let owner = format!("console-{}:{}", OperationId::new(), "b".repeat(64));
    let other_saved = request(
        &f.root,
        Request::ConsoleAction {
            binding: other_binding.clone(),
            generation,
            owner: owner.clone(),
            action: serde_json::from_value(json!({"action":"saved_get","id":id})).unwrap(),
        },
    )
    .unwrap_err();
    assert_eq!(
        supabricks_local::client::diagnostic(&other_saved)["code"],
        "not_found"
    );
    let foreign=request(&f.root,Request::ConsoleAction{binding:other_binding,generation,owner,action:serde_json::from_value(json!({"action":"query","id":OperationId::new(),"target":target,"sql":"SELECT 1","read_only":true,"max_rows":200,"timeout_ms":10000})).unwrap()}).unwrap_err();
    assert_eq!(
        supabricks_local::client::diagnostic(&foreign)["code"],
        "not_found"
    );
    let backup = f.temp.path().join("workspace-backup");
    let out = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["backup", "create"])
        .arg(&backup)
        .arg("--data-dir")
        .arg(&f.root)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        fs::read(backup.join("data").join(&relative)).unwrap(),
        fs::read(&path).unwrap()
    );
    assert!(!backup.join("data/tmp").exists());
    let restored = f.temp.path().join("restored");
    let out = Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .args(["backup", "restore"])
        .arg(&backup)
        .arg("--data-dir")
        .arg(&restored)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        fs::read(restored.join(&relative)).unwrap(),
        fs::read(&path).unwrap()
    );
}

#[test]
fn notebook_routes_require_browser_identity_and_short_session_lifetimes_are_bounded() {
    let fixture = Fixture::new();
    let (origin, token) = parts(&fixture.open());
    let headers = [
        ("Origin", origin.as_str()),
        ("X-Supabricks-Console", "1"),
        ("Content-Type", "application/json"),
    ];
    for lifetime in [0, 9, 28801, u64::MAX] {
        assert_eq!(
            http(
                &origin,
                "POST",
                "/api/session",
                &headers,
                &json!({"token":token,"lifetime_seconds":lifetime}).to_string()
            )
            .status,
            400
        );
    }
    let reply = http(
        &origin,
        "POST",
        "/api/session",
        &headers,
        &json!({"token":token,"lifetime_seconds":10}).to_string(),
    );
    assert_eq!(reply.status, 200);
    assert!(reply.headers.contains("Max-Age=10"));
    let route = format!(
        "/api/notebooks/{}/1/channels",
        supabricks_core::resource::OperationId::new()
    );
    assert_eq!(http(&origin, "GET", &route, &[], "").status, 403);
    assert_eq!(
        http(&origin, "GET", &route, &[("Origin", origin.as_str())], "").status,
        401
    );
    assert_eq!(
        http(&origin, "POST", "/api/notebooks/ticket", &headers, "{}").status,
        401
    );
    assert!(!reply.headers.to_lowercase().contains("connection: upgrade"));
}
