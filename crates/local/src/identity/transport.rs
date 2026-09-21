//! Loopback-only identity and project-control adapters; data/workload routes stay gated.
use super::*;
use crate::{client, daemon::Request};
use reqwest::Url;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
    time::{Duration, Instant},
};
use tiny_http::{Header, Method, Response, Server, StatusCode};

pub fn call(root: &Path, command: AuthCommand) -> Result<Value> {
    client::request(
        root,
        Request::IdentityAuth {
            api_version: VERSION,
            command,
        },
    )
}
pub fn read_private(path: &Path) -> Result<Value> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.mode() & 0o077 != 0
        || metadata.nlink() != 1
        || metadata.len() > 65536
    {
        return Err(denied());
    }
    let mut bytes = Vec::new();
    file.take(65537).read_to_end(&mut bytes)?;
    if bytes.len() > 65536 {
        return Err(denied());
    }
    serde_json::from_slice(&bytes).map_err(|_| denied())
}
pub fn write_private(path: &Path, value: &Value) -> Result<()> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    file.write_all(&serde_json::to_vec(value)?)?;
    file.sync_all()?;
    Ok(())
}
fn listener(redirect: &str) -> Result<(Server, String)> {
    let url = Url::parse(redirect).map_err(|_| denied())?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.path() != "/auth/v1/callback"
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(invalid(
            "authentication preview requires http://127.0.0.1:PORT/auth/v1/callback",
        ));
    }
    let port = url.port().filter(|p| *p != 0).ok_or_else(denied)?;
    let host = format!("127.0.0.1:{port}");
    Ok((
        Server::http(&host).map_err(|_| invalid("authentication callback listener unavailable"))?,
        host,
    ))
}
fn single<'a>(request: &'a tiny_http::Request, name: &str) -> Option<&'a str> {
    let mut values = request
        .headers()
        .iter()
        .filter(|h| h.field.as_str().as_str().eq_ignore_ascii_case(name));
    let value = values.next()?.value.as_str();
    if values.next().is_some() {
        None
    } else {
        Some(value)
    }
}
fn cookie(request: &tiny_http::Request, name: &str) -> Option<String> {
    let mut values = single(request, "cookie")?
        .split(';')
        .filter_map(|v| v.trim().split_once('='))
        .filter(|(key, _)| *key == name);
    let (_, value) = values.next()?;
    if values.next().is_some() {
        None
    } else {
        Some(value.into())
    }
}
fn fields(value: &str) -> Result<BTreeMap<String, String>> {
    if value.len() > 16384 {
        return Err(denied());
    }
    let mut result = BTreeMap::new();
    for (key, value) in reqwest::Url::parse(&format!("http://127.0.0.1/?{value}"))
        .map_err(|_| denied())?
        .query_pairs()
    {
        if result
            .insert(key.into_owned(), value.into_owned())
            .is_some()
        {
            return Err(denied());
        }
    }
    Ok(result)
}
fn callback(request: &tiny_http::Request, host: &str) -> Result<(String, String)> {
    if request.method() != &Method::Get || single(request, "host") != Some(host) {
        return Err(denied());
    }
    let (path, query) = request.url().split_once('?').ok_or_else(denied)?;
    if path != "/auth/v1/callback" || query.contains('#') {
        return Err(denied());
    }
    let fields = fields(query)?;
    if fields
        .keys()
        .any(|k| !matches!(k.as_str(), "state" | "code" | "session_state" | "iss"))
    {
        return Err(denied());
    }
    let state = fields
        .get("state")
        .filter(|s| !s.is_empty())
        .ok_or_else(denied)?
        .clone();
    let code = fields
        .get("code")
        .filter(|s| !s.is_empty())
        .ok_or_else(denied)?
        .clone();
    Ok((state, code))
}
fn reply(
    request: tiny_http::Request,
    status: u16,
    body: &str,
    cookies: &[String],
    location: Option<&str>,
) {
    let mut response = Response::from_string(body).with_status_code(StatusCode(status));
    for (name, value) in [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Content-Type-Options", "nosniff"),
        (
            "Content-Security-Policy",
            "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        ),
    ] {
        response.add_header(Header::from_bytes(name, value).unwrap());
    }
    for cookie in cookies {
        response.add_header(Header::from_bytes("Set-Cookie", cookie.as_str()).unwrap());
    }
    if let Some(location) = location {
        if let Ok(header) = Header::from_bytes("Location", location) {
            response.add_header(header);
        }
    }
    let _ = request.respond(response);
}
fn escaped(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}
fn session_fields(value: Value) -> Result<(String, Channel)> {
    let token = value["token"].as_str().ok_or_else(denied)?.to_owned();
    let channel: Channel =
        serde_json::from_value(value["channel"].clone()).map_err(|_| denied())?;
    if channel == Channel::Browser {
        return Err(denied());
    }
    Ok((token, channel))
}
pub fn whoami(root: &Path, path: &Path, logout: bool) -> Result<Value> {
    let (token, channel) = session_fields(read_private(path)?)?;
    call(
        root,
        if logout {
            AuthCommand::Logout {
                token,
                channel,
                csrf: None,
            }
        } else {
            AuthCommand::Authenticate {
                token,
                channel,
                csrf: None,
            }
        },
    )
}
/// Same typed server-side admission path for CLI, MCP and browser requests.
pub fn control(root: &Path, path: &Path, command: crate::authorization::Command) -> Result<Value> {
    let (token, channel) = session_fields(read_private(path)?)?;
    control_credential(root, token, channel, None, command)
}
fn control_credential(
    root: &Path,
    token: String,
    channel: Channel,
    csrf: Option<String>,
    command: crate::authorization::Command,
) -> Result<Value> {
    client::request(
        root,
        Request::Authorized {
            envelope: crate::authorization::Envelope {
                api_version: crate::authorization::VERSION,
                token,
                channel,
                csrf,
                command,
            },
        },
    )
}
fn json_reply(request: tiny_http::Request, status: u16, value: Value) {
    let mut response =
        Response::from_string(value.to_string()).with_status_code(StatusCode(status));
    for (name, value) in [
        ("Content-Type", "application/json"),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        (
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'",
        ),
        ("Referrer-Policy", "no-referrer"),
    ] {
        response.add_header(Header::from_bytes(name, value).unwrap());
    }
    let _ = request.respond(response);
}
pub fn login(root: &Path, provider: &str, redirect: &str, output: &Path) -> Result<Value> {
    if output.exists() {
        return Err(invalid("session output must be a new private file"));
    }
    let (server, host) = listener(redirect)?;
    let binding = secret()?;
    let start = call(
        root,
        AuthCommand::Begin {
            provider: provider.into(),
            redirect: redirect.into(),
            binding: binding.clone(),
            channel: Channel::Cli,
        },
    )?;
    eprintln!(
        "Open this URL to sign in: {}",
        start["authorization_url"].as_str().ok_or_else(denied)?
    );
    let until = Instant::now() + Duration::from_secs(300);
    while Instant::now() < until {
        let Some(request) = server.recv_timeout(Duration::from_secs(1))? else {
            continue;
        };
        let result = callback(&request, &host).and_then(|(state, code)| {
            call(
                root,
                AuthCommand::Complete {
                    state,
                    code,
                    binding: binding.clone(),
                    redirect: redirect.into(),
                    channel: Channel::Cli,
                },
            )
        });
        match result {
            Ok(session) => {
                if let Err(error) = write_private(output, &session) {
                    if let Ok((token, channel)) = session_fields(session) {
                        let _ = call(
                            root,
                            AuthCommand::Logout {
                                token,
                                channel,
                                csrf: None,
                            },
                        );
                    }
                    reply(
                        request,
                        500,
                        "Session could not be saved. Sign in again.",
                        &[],
                        None,
                    );
                    return Err(error);
                }
                reply(
                    request,
                    200,
                    "Signed in. You can close this page.",
                    &[],
                    None,
                );
                return Ok(
                    json!({"signed_in":true,"principal_id":session["principal_id"],"expires_ms":session["expires_ms"]}),
                );
            }
            Err(_) => reply(request, 403, "Sign-in refused.", &[], None),
        }
    }
    Err(invalid("sign-in expired"))
}
/// Authentication-only browser preview, intentionally independent of local-owner
/// console tickets. There is no route forwarding to project/SQL/notebook APIs.
pub fn browser(root: &Path, provider: &str, redirect: &str) -> Result<()> {
    let (server, host) = listener(redirect)?;
    let origin = format!("http://{host}");
    // Random cookie names avoid conflicts with other loopback applications.
    let suffix = &secret()?[..16];
    let pending_name = format!("sb_login_{suffix}");
    let session_name = format!("sb_identity_{suffix}");
    let csrf_name = format!("sb_csrf_{suffix}");
    let set_cookie = |name: &str, value: &str, age: i64| {
        format!("{name}={value}; Path=/auth/v1; HttpOnly; SameSite=Lax; Max-Age={age}")
    };
    eprintln!("Identity preview: {origin}/auth/v1/");
    loop {
        let request = server.recv()?;
        if single(&request, "host") != Some(&host) {
            reply(request, 403, "Unexpected host.", &[], None);
            continue;
        }
        let path = request.url().split('?').next().unwrap_or("");
        if request.method() == &Method::Get && request.url() == "/auth/v1/" {
            let binding = secret()?;
            let body = format!(
                "<!doctype html><title>Supabricks sign in</title><h1>Identity preview</h1><p>Project and data access are not enabled.</p><form method=post action=/auth/v1/login><input type=hidden name=csrf value=\"{binding}\"><button>Sign in</button></form>"
            );
            reply(
                request,
                200,
                &body,
                &[set_cookie(&pending_name, &binding, 300)],
                None,
            );
            continue;
        }
        if request.method() == &Method::Get && path == "/auth/v1/callback" {
            let result = callback(&request, &host).and_then(|(state, code)| {
                call(
                    root,
                    AuthCommand::Complete {
                        state,
                        code,
                        binding: cookie(&request, &pending_name).ok_or_else(denied)?,
                        redirect: redirect.into(),
                        channel: Channel::Browser,
                    },
                )
            });
            match result {
                Ok(session) => {
                    let csrf = session["csrf"].as_str().ok_or_else(denied)?;
                    let token = session["token"].as_str().ok_or_else(denied)?;
                    // Callback URL is immediately cleared; no provider token enters HTML.
                    reply(
                        request,
                        303,
                        "",
                        &[
                            set_cookie(&session_name, token, 3600),
                            set_cookie(&csrf_name, csrf, 3600),
                            set_cookie(&pending_name, "", 0),
                        ],
                        Some("/auth/v1/session"),
                    );
                }
                Err(_) => reply(request, 403, "Sign-in refused.", &[], None),
            }
            continue;
        }
        if request.method() == &Method::Get && request.url() == "/auth/v1/session" {
            let csrf = cookie(&request, &csrf_name).unwrap_or_default();
            let result = call(
                root,
                AuthCommand::Authenticate {
                    token: cookie(&request, &session_name).unwrap_or_default(),
                    channel: Channel::Browser,
                    csrf: Some(csrf.clone()),
                },
            );
            match result {
                Ok(context) => reply(
                    request,
                    200,
                    &format!(
                        "<!doctype html><title>Supabricks identity</title><pre>{}</pre><form method=post action=/auth/v1/logout><input type=hidden name=csrf value=\"{}\"><button>Sign out</button></form>",
                        escaped(&context.to_string()),
                        escaped(&csrf)
                    ),
                    &[],
                    None,
                ),
                Err(_) => reply(
                    request,
                    403,
                    "Session unavailable. Sign in again.",
                    &[],
                    None,
                ),
            }
            continue;
        }
        if path == "/auth/v1/control" {
            let valid = request.method() == &Method::Post
                && request.url() == path
                && single(&request, "origin") == Some(&origin)
                && single(&request, "content-type") == Some("application/json")
                && request.body_length().is_some_and(|n| n <= 49152);
            if !valid {
                json_reply(request, 403, json!({"error":"Request refused"}));
                continue;
            }
            let token = cookie(&request, &session_name).unwrap_or_default();
            let csrf = single(&request, "x-csrf-token").map(str::to_owned);
            let mut request = request;
            let mut bytes = Vec::new();
            if request
                .as_reader()
                .take(49153)
                .read_to_end(&mut bytes)
                .is_err()
                || bytes.len() > 49152
            {
                json_reply(request, 403, json!({"error":"Request refused"}));
                continue;
            }
            let command = serde_json::from_slice(&bytes).map_err(|_| denied());
            let result = command.and_then(|command| {
                control_credential(root, token, Channel::Browser, csrf, command)
            });
            match result {
                Ok(value) => json_reply(request, 200, value),
                Err(_) => json_reply(request, 403, json!({"error":"Project action refused"})),
            }
            continue;
        }
        if request.method() != &Method::Post
            || single(&request, "origin") != Some(&origin)
            || request.url().contains('?')
            || !matches!(path, "/auth/v1/login" | "/auth/v1/logout")
        {
            reply(request, 403, "Request refused.", &[], None);
            continue;
        }
        let login = path == "/auth/v1/login";
        let binding = cookie(&request, &pending_name).unwrap_or_default();
        let token = cookie(&request, &session_name).unwrap_or_default();
        let csrf_cookie = cookie(&request, &csrf_name).unwrap_or_default();
        let mut request = request;
        let mut body = String::new();
        if request.body_length().is_none_or(|n| n > 4096)
            || request
                .as_reader()
                .take(4097)
                .read_to_string(&mut body)
                .is_err()
            || body.len() > 4096
        {
            reply(request, 403, "Request refused.", &[], None);
            continue;
        }
        let posted = fields(&body)
            .ok()
            .and_then(|f| f.get("csrf").cloned())
            .unwrap_or_default();
        if posted.len() != 64 || hash(&posted) != hash(if login { &binding } else { &csrf_cookie })
        {
            reply(request, 403, "Request refused.", &[], None);
            continue;
        }
        let command = if login {
            AuthCommand::Begin {
                provider: provider.into(),
                redirect: redirect.into(),
                binding,
                channel: Channel::Browser,
            }
        } else {
            AuthCommand::Logout {
                token,
                channel: Channel::Browser,
                csrf: Some(posted),
            }
        };
        match call(root, command) {
            Ok(result) if login => {
                reply(request, 303, "", &[], result["authorization_url"].as_str())
            }
            Ok(_) => reply(
                request,
                303,
                "",
                &[
                    set_cookie(&session_name, "", 0),
                    set_cookie(&csrf_name, "", 0),
                ],
                Some("/auth/v1/"),
            ),
            Err(_) => reply(request, 403, "Authentication unavailable.", &[], None),
        }
    }
}
/// Authenticated identity and project control tools; all capabilities are checked by the daemon.
/// Credential paths are process configuration, never tool arguments or results.
pub fn mcp(root: &Path, path: &Path) -> Result<()> {
    use std::io::BufRead;
    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    let mut output = std::io::stdout().lock();
    loop {
        let mut wire = String::new();
        if input.by_ref().take(65537).read_line(&mut wire)? == 0 {
            return Ok(());
        }
        if wire.len() > 65536 || !wire.ends_with('\n') {
            return Err(invalid("invalid MCP frame"));
        }
        let request: Value =
            serde_json::from_str(&wire).map_err(|_| invalid("invalid MCP JSON"))?;
        if request.get("id").is_none() {
            continue;
        }
        let result = match whoami(root, path, false) {
            Err(_) => json!({"error":{"code":-32001,"message":"Authentication required"}}),
            Ok(context) => match request["method"].as_str() {
                Some("initialize") => {
                    json!({"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"supabricks-identity","version":"1"}}})
                }
                Some("tools/list") => {
                    json!({"result":{"tools":[{"name":"identity_whoami","description":"Inspect the authenticated identity.","inputSchema":{"type":"object","properties":{},"additionalProperties":false}},{"name":"project_control","description":"Project roles, immutable source revisions and execution admission; catalog/PG and workload launch remain disabled.","inputSchema":{"type":"object","properties":{"command":{"type":"object"}},"required":["command"],"additionalProperties":false}}]}})
                }
                Some("tools/call")
                    if request["params"]["name"] == "identity_whoami"
                        && request["params"]["arguments"]
                            .as_object()
                            .is_none_or(|a| a.is_empty()) =>
                {
                    json!({"result":{"content":[{"type":"text","text":context.to_string()}],"isError":false}})
                }
                Some("tools/call") if request["params"]["name"] == "project_control" => {
                    let arguments = &request["params"]["arguments"];
                    let result = if arguments
                        .as_object()
                        .is_some_and(|a| a.len() == 1 && a.contains_key("command"))
                    {
                        serde_json::from_value(arguments["command"].clone())
                            .map_err(|_| denied())
                            .and_then(|command| control(root, path, command))
                    } else {
                        Err(denied())
                    };
                    match result {
                        Ok(value) => {
                            json!({"result":{"content":[{"type":"text","text":value.to_string()}],"isError":false}})
                        }
                        Err(_) => {
                            json!({"result":{"content":[{"type":"text","text":"Project action refused"}],"isError":true}})
                        }
                    }
                }
                _ => json!({"error":{"code":-32601,"message":"Method unavailable"}}),
            },
        };
        let mut response = result;
        response["jsonrpc"] = json!("2.0");
        response["id"] = request["id"].clone();
        writeln!(output, "{response}")?;
        output.flush()?;
    }
}
