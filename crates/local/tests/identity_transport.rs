//! Exercise the shipped daemon/CLI and browser adapter over real sockets/TLS.
use reqwest::blocking::Client;
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use supabricks_local::{
    client,
    daemon::Request,
    identity::{AdminCommand, AuthCommand, Channel, oidc::Config, transport},
};
struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
struct Fixture {
    temp: tempfile::TempDir,
    root: PathBuf,
    _daemon: Process,
    _provider: Process,
    issuer: String,
    http: Client,
    config: Config,
}
impl Fixture {
    fn new() -> Self {
        let temp = tempfile::Builder::new()
            .prefix("sb-identity-")
            .tempdir_in("/tmp")
            .unwrap();
        let root = temp.path().join("state");
        let daemon = Process(
            Command::new(env!("CARGO_BIN_EXE_supabricks"))
                .args(["daemon", "--data-dir"])
                .arg(&root)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let until = Instant::now() + Duration::from_secs(10);
        while !root.join("control.sock").exists() {
            assert!(Instant::now() < until);
            std::thread::sleep(Duration::from_millis(10));
        }
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/identity");
        let mut provider = Process(
            Command::new("python3")
                .arg(fixture.join("provider.py"))
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let mut issuer = String::new();
        BufReader::new(provider.0.stdout.take().unwrap())
            .read_line(&mut issuer)
            .unwrap();
        let issuer = issuer.trim().to_owned();
        let ca_pem = fs::read_to_string(fixture.join("cert.pem")).unwrap();
        let http = Client::builder()
            .timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::none())
            .add_root_certificate(reqwest::Certificate::from_pem(ca_pem.as_bytes()).unwrap())
            .build()
            .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let config = Config {
            issuer: issuer.clone(),
            client_id: "platform".into(),
            client_secret: "fixture-secret".into(),
            introspection_url: format!("{issuer}/introspect"),
            redirects: vec![format!("http://127.0.0.1:{port}/auth/v1/callback")],
            ca_pem: Some(ca_pem),
        };
        client::request(
            &root,
            Request::IdentityAdmin {
                command: AdminCommand::Configure {
                    provider: "fixture".into(),
                    config: config.clone(),
                },
            },
        )
        .unwrap();
        Self {
            temp,
            root,
            _daemon: daemon,
            _provider: provider,
            issuer,
            http,
            config,
        }
    }
    fn spawn(&self, args: &[&str]) -> Process {
        Process(
            Command::new(env!("CARGO_BIN_EXE_supabricks"))
                .args(args)
                .arg("--data-dir")
                .arg(&self.root)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        )
    }
}
fn authorization_project(f: &Fixture, actor: &str) -> String {
    use supabricks_core::resource::ProjectId;
    use supabricks_local::{
        authorization::{AdminCommand as Policy, Role, Subject},
        project::ProjectConfig,
    };
    let config = ProjectConfig {
        id: ProjectId::new(),
        name: "governed".into(),
        format_version: 1,
    };
    client::request(
        &f.root,
        Request::RegisterProject {
            config: config.clone(),
        },
    )
    .unwrap();
    let db = rusqlite::Connection::open_with_flags(
        f.root.join("state.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let deployment: String = db
        .query_row(
            "SELECT id FROM deployments WHERE runtime_project_id=?1",
            [config.id.to_string()],
            |r| r.get(0),
        )
        .unwrap();
    drop(db);
    client::request(
        &f.root,
        Request::AuthorizationAdmin {
            command: Policy::SetRole {
                deployment: deployment.clone(),
                subject: Subject::Principal(actor.into()),
                role: Some(Role::Editor),
                expected_policy: 1,
                key: "initial-role".into(),
            },
        },
    )
    .unwrap();
    deployment
}
fn stderr_line(process: &mut Process) -> String {
    let mut line = String::new();
    BufReader::new(process.0.stderr.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    line
}
#[test]
fn identity_cli_login_private_output_and_authenticated_mcp_never_fall_back() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let output = f.temp.path().join("session.json");
    let mut login = f.spawn(&[
        "identity",
        "login",
        "--provider",
        "fixture",
        "--redirect",
        &f.config.redirects[0],
        "--output",
        output.to_str().unwrap(),
    ]);
    let line = stderr_line(&mut login);
    let auth = line
        .trim()
        .strip_prefix("Open this URL to sign in: ")
        .unwrap();
    let authorize = f.http.get(auth).send().unwrap();
    let callback = authorize.headers()["location"].to_str().unwrap();
    let response = f.http.get(callback).send().unwrap();
    assert_eq!(response.status(), 200);
    assert!(login.0.wait().unwrap().success());
    assert_eq!(
        fs::metadata(&output).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let session = transport::read_private(&output).unwrap();
    let token = session["token"].as_str().unwrap();
    let context = transport::whoami(&f.root, &output, false).unwrap();
    assert_eq!(context["actor_id"], session["principal_id"]);
    assert_eq!(context["actor_id"], context["effective_principal_id"]);
    use supabricks_local::authorization::{AdminCommand as Policy, Command as Control, Subject};
    let deployment = authorization_project(&f, context["actor_id"].as_str().unwrap());
    let request_path = f.temp.path().join("control.json");
    transport::write_private(
        &request_path,
        &json!({"action":"project","deployment":deployment}),
    )
    .unwrap();
    let mut control = f.spawn(&[
        "identity",
        "control",
        "--session-file",
        output.to_str().unwrap(),
        "--request-file",
        request_path.to_str().unwrap(),
    ]);
    let mut cli_result = String::new();
    BufReader::new(control.0.stdout.take().unwrap())
        .read_line(&mut cli_result)
        .unwrap();
    assert!(control.0.wait().unwrap().success());
    assert_eq!(
        serde_json::from_str::<Value>(&cli_result).unwrap()["deployment_id"],
        deployment
    );
    assert!(!cli_result.contains(token));
    let mut control_mcp = f.spawn(&[
        "identity",
        "mcp",
        "--session-file",
        output.to_str().unwrap(),
    ]);
    let mut input = control_mcp.0.stdin.take().unwrap();
    writeln!(input,"{}",json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"project_control","arguments":{"command":{"action":"project","deployment":deployment}}}})).unwrap();
    drop(input);
    let mut result = String::new();
    BufReader::new(control_mcp.0.stdout.take().unwrap())
        .read_line(&mut result)
        .unwrap();
    assert!(control_mcp.0.wait().unwrap().success());
    assert!(result.contains(&deployment));
    assert!(!result.contains(token));
    client::request(
        &f.root,
        Request::AuthorizationAdmin {
            command: Policy::SetRole {
                deployment: deployment.clone(),
                subject: Subject::Principal(context["actor_id"].as_str().unwrap().into()),
                role: None,
                expected_policy: 2,
                key: "revoke".into(),
            },
        },
    )
    .unwrap();
    assert!(transport::control(&f.root, &output, Control::Project { deployment }).is_err());

    assert!(
        client::request(
            &f.root,
            Request::IdentityAuth {
                api_version: 999,
                command: AuthCommand::Authenticate {
                    token: token.into(),
                    channel: Channel::Cli,
                    csrf: None
                }
            }
        )
        .is_err()
    );
    let mut mcp = f.spawn(&[
        "identity",
        "mcp",
        "--session-file",
        output.to_str().unwrap(),
    ]);
    let mut input = mcp.0.stdin.take().unwrap();
    writeln!(input,"{}",json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"identity_whoami","arguments":{}}})).unwrap();
    drop(input);
    let mut line = String::new();
    BufReader::new(mcp.0.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert!(!line.contains(token));
    assert!(line.contains(context["actor_id"].as_str().unwrap()));
    assert!(mcp.0.wait().unwrap().success());
    f.http
        .get(format!("{}/control?outage=true", f.issuer))
        .send()
        .unwrap();
    let root = f.root.clone();
    let saved = output.clone();
    let pending = std::thread::spawn(move || transport::whoami(&root, &saved, false));
    let start = Instant::now();
    client::request(&f.root, Request::Status).unwrap();
    assert!(
        start.elapsed() < Duration::from_secs(2),
        "IdP I/O blocked lifecycle processing"
    );
    assert!(pending.join().unwrap().is_err());
    transport::whoami(&f.root, &output, true).unwrap();
    f.http
        .get(format!("{}/control?outage=false", f.issuer))
        .send()
        .unwrap();
    assert!(transport::whoami(&f.root, &output, false).is_err());
    fs::set_permissions(&output, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(transport::read_private(&output).is_err());
}
fn cookies(response: &reqwest::blocking::Response) -> String {
    response
        .headers()
        .get_all("set-cookie")
        .iter()
        .map(|v| v.to_str().unwrap().split(';').next().unwrap())
        .collect::<Vec<_>>()
        .join("; ")
}
fn csrf(html: &str) -> String {
    html.split("name=csrf value=\"")
        .nth(1)
        .unwrap()
        .split('"')
        .next()
        .unwrap()
        .into()
}
#[test]
fn identity_browser_pkce_cookie_csrf_logout_and_product_gate() {
    let f = Fixture::new();
    let mut browser = f.spawn(&[
        "identity",
        "browser",
        "--provider",
        "fixture",
        "--redirect",
        &f.config.redirects[0],
    ]);
    let line = stderr_line(&mut browser);
    let url = line.trim().strip_prefix("Identity preview: ").unwrap();
    let origin = url.trim_end_matches("/auth/v1/");
    let home = f.http.get(url).send().unwrap();
    let pending = cookies(&home);
    let form = csrf(&home.text().unwrap());
    let refused = f
        .http
        .post(format!("{origin}/auth/v1/login"))
        .header("Origin", "https://attacker.example")
        .header("Cookie", &pending)
        .form(&[("csrf", &form)])
        .send()
        .unwrap();
    assert_eq!(refused.status(), 403);
    let start = f
        .http
        .post(format!("{origin}/auth/v1/login"))
        .header("Origin", origin)
        .header("Cookie", &pending)
        .form(&[("csrf", &form)])
        .send()
        .unwrap();
    assert_eq!(start.status(), 303);
    let authorization = f
        .http
        .get(start.headers()["location"].to_str().unwrap())
        .send()
        .unwrap();
    let callback = authorization.headers()["location"].to_str().unwrap();
    assert_eq!(f.http.get(callback).send().unwrap().status(), 403);
    assert_eq!(
        f.http
            .get(format!("{callback}&state=duplicate"))
            .header("Cookie", &pending)
            .send()
            .unwrap()
            .status(),
        403
    );
    let complete = f
        .http
        .get(callback)
        .header("Cookie", &pending)
        .send()
        .unwrap();
    assert_eq!(complete.status(), 303);
    let session = cookies(&complete);
    assert_eq!(complete.headers()["location"], "/auth/v1/session");
    for header in complete.headers().get_all("set-cookie") {
        let header = header.to_str().unwrap();
        assert!(header.contains("HttpOnly"));
        assert!(header.contains("SameSite=Lax"));
    }
    assert_eq!(
        f.http
            .get(callback)
            .header("Cookie", &pending)
            .send()
            .unwrap()
            .status(),
        403
    );
    let page = f
        .http
        .get(format!("{origin}/auth/v1/session"))
        .header("Cookie", &session)
        .send()
        .unwrap();
    assert_eq!(page.status(), 200);
    let html = page.text().unwrap();
    let form = csrf(&html);
    let identity = client::request(
        &f.root,
        Request::IdentityAdmin {
            command: AdminCommand::Status,
        },
    )
    .unwrap();
    let actor = identity["principals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["kind"] == "user")
        .unwrap()["id"]
        .as_str()
        .unwrap();
    let deployment = authorization_project(&f, actor);
    let control = |body: Value, csrf: &str, request_origin: &str| {
        f.http
            .post(format!("{origin}/auth/v1/control"))
            .header("Origin", request_origin)
            .header("Cookie", &session)
            .header("X-CSRF-Token", csrf)
            .header("Content-Type", "application/json")
            .body(body.to_string())
            .send()
            .unwrap()
    };
    assert_eq!(
        control(json!({"action":"projects"}), "wrong", origin).status(),
        403
    );
    assert_eq!(
        control(
            json!({"action":"projects"}),
            &form,
            "https://attacker.example"
        )
        .status(),
        403
    );
    let result = control(
        json!({"action":"project","deployment":deployment}),
        &form,
        origin,
    );
    assert_eq!(result.status(), 200);
    assert_eq!(result.headers()["content-type"], "application/json");
    assert_eq!(
        serde_json::from_str::<Value>(&result.text().unwrap()).unwrap()["deployment_id"],
        deployment
    );
    assert_eq!(
        control(
            json!({"action":"projects","actor_id":"owner"}),
            &form,
            origin
        )
        .status(),
        403
    );
    assert_eq!(control(json!({"action":"save_source","deployment":deployment,"asset":"query","kind":"sql","contents":"select 1","expected_head":null,"expected_policy":2,"key":"browser-save"}),&form,origin).status(),200);
    assert_eq!(
        control(
            json!({"action":"unavailable","deployment":deployment,"operation":"postgres_read"}),
            &form,
            origin
        )
        .status(),
        403
    );

    assert!(!html.contains("access_token"));
    assert!(!html.contains("fixture-secret"));
    assert_eq!(
        f.http
            .get(format!("{origin}/api/projects"))
            .header("Cookie", &session)
            .send()
            .unwrap()
            .status(),
        403
    );
    assert_eq!(
        f.http
            .post(format!("{origin}/auth/v1/logout"))
            .header("Origin", origin)
            .header("Cookie", &session)
            .form(&[("csrf", "wrong")])
            .send()
            .unwrap()
            .status(),
        403
    );
    assert_eq!(
        f.http
            .post(format!("{origin}/auth/v1/logout"))
            .header("Origin", origin)
            .header("Cookie", &session)
            .form(&[("csrf", form)])
            .send()
            .unwrap()
            .status(),
        303
    );
    assert_eq!(
        f.http
            .get(format!("{origin}/auth/v1/session"))
            .header("Cookie", &session)
            .send()
            .unwrap()
            .status(),
        403
    );
}
