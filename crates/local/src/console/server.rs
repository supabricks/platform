mod notebooks;
use super::{
    Config,
    assets::{Assets, VERSION},
    secret,
};
use crate::{
    client,
    daemon::Request,
    store::{Result, error::invalid},
    supervisor,
};
use http_body_util::{BodyExt, Full, Limited};
use hyper::{
    Method, Request as HttpRequest, Response,
    body::{Bytes, Incoming},
    header::{HeaderMap, HeaderValue},
    service::service_fn,
};
use hyper_util::rt::{TokioIo, TokioTimer};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    convert::Infallible,
    fs,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{net::TcpListener, sync::Semaphore};

type Reply = Response<Full<Bytes>>;
struct Session {
    cancel: tokio::sync::watch::Sender<bool>,
    csrf: String,
    expires: Instant,
}
struct State {
    config: Config,
    origin: String,
    host: String,
    cookie: String,
    assets: Assets,
    sessions: Mutex<BTreeMap<String, Session>>,
    tickets: Mutex<BTreeMap<String, notebooks::Ticket>>,
    channels: Arc<Semaphore>,
    exchange: Mutex<()>,
}
fn response(status: u16, mime: &str, bytes: impl Into<Bytes>) -> Reply {
    Response::builder().status(status)
        .header("Content-Type", mime)
        .header("Connection", "close")
        .header("Cache-Control", "no-store")
        .header("X-Content-Type-Options", "nosniff")
        .header("Referrer-Policy", "no-referrer")
        .header("Cross-Origin-Resource-Policy", "same-origin")
        .header("X-Frame-Options", "DENY")
        .header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        .body(Full::new(bytes.into())).unwrap()
}
fn json_response(status: u16, value: Value) -> Reply {
    response(
        status,
        "application/json",
        serde_json::to_vec(&value).unwrap(),
    )
}
fn fail(status: u16, message: &str) -> Reply {
    json_response(
        status,
        json!({"api_version":VERSION,"error":{"message":message}}),
    )
}
fn single<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    let mut all = headers.get_all(name).iter();
    let first = all.next()?.to_str().ok()?;
    if all.next().is_some() {
        None
    } else {
        Some(first)
    }
}
fn session_id(headers: &HeaderMap, cookie: &str) -> Option<String> {
    let mut found = None;
    for header in headers.get_all("cookie") {
        for part in header.to_str().ok()?.split(';') {
            if let Some((name, value)) = part.trim().split_once('=')
                && name == cookie
            {
                if found.is_some() {
                    return None;
                }
                found = Some(value.to_owned());
            }
        }
    }
    found
}
impl State {
    async fn overview(&self) -> Result<Value> {
        let config = self.config.clone();
        tokio::task::spawn_blocking(move || {
            client::request_timeout(
                &config.root,
                Request::ConsoleOverview {
                    binding: config.binding,
                    generation: config.generation,
                },
                Duration::from_secs(2),
            )
        })
        .await
        .map_err(|_| invalid("console worker unavailable"))?
    }
    async fn ingest(&self, id: &str, command: super::ingestion::Command) -> Result<Value> {
        let config = self.config.clone();
        let owner = format!("{}:{id}", config.instance);
        tokio::task::spawn_blocking(move || {
            client::request_timeout(
                &config.root,
                Request::ConsoleAction {
                    binding: config.binding,
                    generation: config.generation,
                    owner,
                    action: super::workspace::Command::Ingest { command },
                },
                Duration::from_secs(2),
            )
        })
        .await
        .map_err(|_| invalid("upload bridge unavailable"))?
    }
    async fn upload(
        &self,
        id: &str,
        source: crate::ingest::SourceId,
        mut body: Incoming,
    ) -> Result<Value> {
        use super::ingestion::Command;
        // Verify binding before reading any body, including an empty body.
        let slot = self.ingest(id, Command::Source { source }).await?;
        if slot["received"] != 0 || slot["status"]["source"]["state"] != "receiving" {
            return Err(invalid("upload slot is not empty"));
        }
        let mut offset = 0;
        while let Some(frame) = tokio::time::timeout(Duration::from_secs(10), body.frame())
            .await
            .map_err(|_| invalid("upload stalled"))?
        {
            let frame = frame.map_err(|_| invalid("upload interrupted"))?;
            if let Ok(bytes) = frame.into_data() {
                for chunk in bytes.chunks(24576) {
                    self.ingest(
                        id,
                        Command::Chunk {
                            source,
                            offset,
                            hex: hex::encode(chunk),
                        },
                    )
                    .await?;
                    offset += chunk.len() as u64;
                }
            }
        }
        if slot["expected"].as_u64() != Some(offset) {
            return Err(invalid("upload incomplete"));
        }
        Ok(json!({"received":offset}))
    }
    fn authenticated(&self, headers: &HeaderMap) -> Option<(String, String)> {
        let id = session_id(headers, &self.cookie)?;
        let mut sessions = self.sessions.lock().unwrap();
        sessions.retain(|_, session| session.expires > Instant::now());
        sessions.get(&id).map(|s| (id, s.csrf.clone()))
    }
    async fn handle(self: Arc<Self>, request: HttpRequest<Incoming>) -> Reply {
        if single(request.headers(), "host") != Some(self.host.as_str())
            || request.uri().query().is_some()
            || request.uri().authority().is_some()
        {
            return fail(403, "Unexpected console host or URL");
        }
        let origin = single(request.headers(), "origin");
        if (request.headers().contains_key("origin") && origin != Some(self.origin.as_str()))
            || single(request.headers(), "sec-fetch-site")
                .is_some_and(|s| !matches!(s, "same-origin" | "none"))
        {
            return fail(403, "Cross-origin console requests are refused");
        }
        let path = request.uri().path().to_owned();
        if !path.starts_with("/api/") {
            if request.method() != Method::GET {
                return fail(405, "Method not allowed");
            }
            let path = if path == "/" { "/index.html" } else { &path };
            return match self.assets.files.get(path) {
                Some((mime, bytes)) if path == "/index.html" => {
                    let nonce = match secret() {
                        Ok(value) => value,
                        Err(_) => return fail(500, "Console style authorization failed"),
                    };
                    let html = String::from_utf8_lossy(bytes).replacen(
                        "<head>",
                        &format!(
                            "<head><meta name=\"supabricks-style-nonce\" content=\"{nonce}\">"
                        ),
                        1,
                    );
                    let mut reply = response(200, mime, html);
                    reply.headers_mut().insert("Content-Security-Policy", HeaderValue::from_str(&format!("default-src 'none'; script-src 'self'; style-src 'self' 'nonce-{nonce}'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")).unwrap());
                    reply
                }
                Some((mime, bytes)) => response(200, mime, bytes.clone()),
                None => fail(404, "Console asset not found"),
            };
        }
        if path.starts_with("/api/notebooks/") && path.ends_with("/channels") {
            return self.notebook_ws(request).await;
        }
        if single(request.headers(), "x-supabricks-console") != Some("1") {
            return fail(
                409,
                "Console API version mismatch; reopen with supabricks console",
            );
        }
        if request.method() == Method::POST && origin != Some(self.origin.as_str()) {
            return fail(403, "Console writes require their exact browser origin");
        }
        if path == "/api/session" && request.method() == Method::POST {
            let existing = self.authenticated(request.headers());
            if single(request.headers(), "content-type") != Some("application/json") {
                return fail(415, "Expected application/json");
            }
            let bytes = match Limited::new(request.into_body(), 4096).collect().await {
                Ok(body) => body.to_bytes(),
                Err(_) => return fail(413, "Console request exceeds 4 KiB"),
            };
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Exchange {
                token: String,
                lifetime_seconds: Option<u64>,
            }
            let body: Exchange = match serde_json::from_slice(&bytes) {
                Ok(body) => body,
                Err(_) => return fail(400, "Invalid launch request"),
            };
            let lifetime = body.lifetime_seconds.unwrap_or(28800);
            if !(10..=28800).contains(&lifetime) {
                return fail(400, "Console session lifetime requires 10–28800 seconds");
            }
            if body.token.len() != 64 || !body.token.bytes().all(|c| c.is_ascii_hexdigit()) {
                return fail(
                    401,
                    "Launch link is invalid or expired; run supabricks console again",
                );
            }
            // Check daemon identity/binding before issuing any session credentials.
            if self.overview().await.is_err() {
                return fail(503, "Runtime changed or unavailable; reopen the console");
            }
            let _guard = self.exchange.lock().unwrap();
            let ticket_path = self
                .config
                .workspace
                .join(format!("ticket-{}.json", body.token));
            let ticket: Value = fs::read(&ticket_path)
                .ok()
                .and_then(|b| serde_json::from_slice(&b).ok())
                .unwrap_or(Value::Null);
            if ticket["token"].as_str() != Some(&body.token)
                || ticket["expires_at_ms"]
                    .as_i64()
                    .is_none_or(|t| t <= chrono::Utc::now().timestamp_millis())
            {
                return fail(
                    401,
                    "Launch link is invalid or expired; run supabricks console again",
                );
            }
            let mut sessions = self.sessions.lock().unwrap();
            sessions.retain(|_, s| s.expires > Instant::now());
            let existing = existing.filter(|(id, _)| sessions.contains_key(id));
            if sessions.len() >= 32 && existing.is_none() {
                return fail(
                    429,
                    "Console session limit reached; close sessions or restart the cell",
                );
            }
            // Reopening in the same browser renews its session instead of leaking
            // an unreachable session slot or invalidating other tabs' CSRF tokens.
            let (id, csrf) = match existing {
                Some(pair) => pair,
                None => match (secret(), secret()) {
                    (Ok(id), Ok(csrf)) => (id, csrf),
                    _ => return fail(500, "Session creation failed"),
                },
            };
            if fs::remove_file(ticket_path).is_err() {
                return fail(401, "Launch link already consumed");
            }
            let cancel = sessions
                .get(&id)
                .map(|s| s.cancel.clone())
                .unwrap_or_else(|| tokio::sync::watch::channel(false).0);
            sessions.insert(
                id.clone(),
                Session {
                    cancel,
                    csrf: csrf.clone(),
                    expires: Instant::now() + Duration::from_secs(lifetime),
                },
            );
            let mut reply = json_response(
                200,
                json!({"api_version":VERSION,"csrf":csrf,"project_id":self.config.binding.project_id}),
            );
            reply.headers_mut().insert(
                "Set-Cookie",
                HeaderValue::from_str(&format!(
                    "{}={id}; HttpOnly; SameSite=Strict; Path=/; Max-Age={lifetime}",
                    self.cookie
                ))
                .unwrap(),
            );
            return reply;
        }
        let Some((id, csrf)) = self.authenticated(request.headers()) else {
            return fail(
                401,
                "Console session expired; run supabricks console to reconnect",
            );
        };
        if request.method() == Method::POST
            && single(request.headers(), "x-supabricks-csrf") != Some(csrf.as_str())
        {
            return fail(403, "Invalid CSRF token");
        }
        if path.starts_with("/api/notebooks/") {
            return self.notebook_http(&id, request).await;
        }
        if let Some(source) = path.strip_prefix("/api/upload/") {
            if request.method() != Method::POST
                || single(request.headers(), "content-type") != Some("application/octet-stream")
            {
                return fail(415, "Expected a POST file stream");
            }
            let source = match source.parse::<crate::ingest::SourceId>() {
                Ok(id) => id,
                Err(_) => return fail(400, "Invalid source ID"),
            };
            let result = self.upload(&id, source, request.into_body()).await;
            return match result {
                Ok(value) => json_response(200, json!({"api_version":VERSION,"value":value})),
                Err(_) => {
                    let _ = self
                        .ingest(&id, super::ingestion::Command::Dispose { source })
                        .await;
                    fail(
                        409,
                        "Upload interrupted, rejected or out of disk space. Select the file again.",
                    )
                }
            };
        }
        if path == "/api/workspace" && request.method() == Method::POST {
            if single(request.headers(), "content-type") != Some("application/json") {
                return fail(415, "Expected application/json");
            }
            let bytes = match Limited::new(request.into_body(), 60000).collect().await {
                Ok(body) => body.to_bytes(),
                Err(_) => return fail(413, "Workspace request exceeds 60 KB"),
            };
            let action: super::workspace::Command = match serde_json::from_slice(&bytes) {
                Ok(action) => action,
                Err(_) => return fail(400, "Invalid workspace command"),
            };
            // Notebook admission verifies a complete environment and starts
            // owned services on the single-writer daemon. Cold macOS filesystem
            // reads can exceed the general two-second control deadline. Allow
            // these commands to finish within the outer eight-second HTTP bound;
            // never resend a mutation after a transport timeout.
            let deadline = if matches!(
                &action,
                super::workspace::Command::Notebook { .. }
                    | super::workspace::Command::Environment { .. }
            ) {
                Duration::from_secs(6)
            } else {
                Duration::from_secs(2)
            };
            let config = self.config.clone();
            let result = tokio::task::spawn_blocking(move || {
                client::request_timeout(
                    &config.root,
                    Request::ConsoleAction {
                        binding: config.binding,
                        generation: config.generation,
                        owner: format!("{}:{id}", config.instance),
                        action,
                    },
                    deadline,
                )
            })
            .await;
            return match result {
                Ok(Ok(value)) => json_response(200, json!({"api_version":VERSION,"value":value})),
                Ok(Err(error)) => {
                    let diagnostic = crate::client::diagnostic(&error);
                    let status = match diagnostic["code"].as_str() {
                        Some("invalid_input") => 400,
                        Some("not_found") => 404,
                        Some("conflict") => 409,
                        _ => 503,
                    };
                    json_response(status, json!({"api_version":VERSION,"error":diagnostic}))
                }
                Err(_) => fail(
                    503,
                    "Workspace worker unavailable; inspect operation before retrying",
                ),
            };
        }
        if path == "/api/logout" && request.method() == Method::POST {
            if single(request.headers(), "x-supabricks-csrf") != Some(csrf.as_str()) {
                return fail(403, "Invalid CSRF token");
            }
            if let Some(session) = self.sessions.lock().unwrap().remove(&id) {
                session.cancel.send_replace(true);
            }
            self.tickets
                .lock()
                .unwrap()
                .retain(|_, ticket| ticket.owner != id);
            // Revoke sockets immediately; the daemon fences kernels even if this
            // request is lost, through its six-second owner heartbeat lease.
            let _ = self
                .notebook_request(&id, crate::notebooks::contract::Transport::Revoke)
                .await;
            let mut reply = json_response(200, json!({"api_version":VERSION,"closed":true}));
            reply.headers_mut().insert(
                "Set-Cookie",
                HeaderValue::from_str(&format!(
                    "{}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
                    self.cookie
                ))
                .unwrap(),
            );
            return reply;
        }
        if path == "/api/session" && request.method() == Method::GET {
            return json_response(
                200,
                json!({"api_version":VERSION,"csrf":csrf,"project_id":self.config.binding.project_id}),
            );
        }
        if path == "/api/overview" && request.method() == Method::GET {
            return match self.overview().await {
                Ok(value) => json_response(200, value),
                Err(_) => fail(
                    503,
                    "Runtime unavailable or project identity changed; run supabricks doctor, then reopen the console",
                ),
            };
        }
        fail(404, "Console action not available")
    }
}

pub(super) async fn serve(config: Config) -> Result<()> {
    // The gated supervisor wrapper records identity before this process runs.
    let own_token = std::env::var("SUPABRICKS_PROCESS_TOKEN")
        .map_err(|_| invalid("console must be launched by its owning daemon"))?;
    if own_token.is_empty() {
        return Err(invalid("missing console process identity"));
    }
    let assets = Assets::load(&config.assets)?;
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let port = listener.local_addr()?.port();
    let state = Arc::new(State {
        host: format!("127.0.0.1:{port}"),
        origin: format!("http://127.0.0.1:{port}"),
        cookie: format!("sb_{}", config.instance.replace('-', "")),
        config: config.clone(),
        assets,
        sessions: Mutex::new(BTreeMap::new()),
        tickets: Mutex::new(BTreeMap::new()),
        channels: Arc::new(Semaphore::new(4)),
        exchange: Mutex::new(()),
    });
    supervisor::write_json(
        &config.workspace.join("ready.json"),
        &json!({"port":port,"instance":config.instance,"pid":std::process::id()}),
    )?;
    let permits = Arc::new(Semaphore::new(16));
    let mut heartbeat = tokio::time::interval(Duration::from_secs(2));
    let mut failures = 0;
    loop {
        tokio::select! {
            _ = heartbeat.tick() => {
                // Fail closed on daemon death/replacement or project-file changes.
                if state.overview().await.is_err() || state.notebook_heartbeat().await.is_err() { failures += 1; } else { failures = 0; }
                if failures >= 2 { return Ok(()); }
            }
            accept = listener.accept() => {
                let (stream, _) = accept?;
                let Ok(permit) = permits.clone().try_acquire_owned() else { drop(stream); continue; };
                let state = state.clone();
                tokio::spawn(async move {
                    let _permit = permit;
                    let service = service_fn(move |request| {
                        let state = state.clone();
                        async move {
                            let timeout = if request.uri().path().starts_with("/api/upload/") {610} else {8};
                            Ok::<_, Infallible>(tokio::time::timeout(Duration::from_secs(timeout),state.handle(request)).await.unwrap_or_else(|_|fail(408,"Console request timed out")))
                        }
                    });
                    let mut builder = hyper::server::conn::http1::Builder::new();
                    builder.keep_alive(true).max_buf_size(16384).max_headers(32)
                        .timer(TokioTimer::new()).header_read_timeout(Duration::from_secs(3));
                    let _ = tokio::time::timeout(Duration::from_secs(615), builder.serve_connection(TokioIo::new(stream), service).with_upgrades()).await;
                });
            }
        }
    }
}
