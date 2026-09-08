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
