//! Same-origin, session-bound Jupyter transport. No caller-supplied upstream URL.
use super::*;
use crate::notebooks::contract::{FRAME_BYTES, Transport};
use futures_util::{SinkExt, StreamExt};
use supabricks_core::resource::OperationId;
use tokio::sync::watch;
use tokio_tungstenite::{
    WebSocketStream,
    tungstenite::{
        self,
        client::IntoClientRequest,
        protocol::{Role, WebSocketConfig},
    },
};

const PROTOCOL: &str = "v1.kernel.websocket.jupyter.org";
pub(super) struct Ticket {
    pub(super) owner: String,
    id: OperationId,
    generation: u64,
    expires: Instant,
}
fn limits() -> WebSocketConfig {
    WebSocketConfig::default()
        .max_message_size(Some(FRAME_BYTES))
        .max_frame_size(Some(FRAME_BYTES))
        .write_buffer_size(0)
        .max_write_buffer_size(FRAME_BYTES + 1024)
}
fn target(path: &str, suffix: &str) -> Option<(OperationId, u64)> {
    let inner = path.strip_prefix("/api/notebooks/")?.strip_suffix(suffix)?;
    let (id, generation) = inner.split_once('/')?;
    if generation.len() > 20 {
        return None;
    }
    Some((id.parse().ok()?, generation.parse().ok()?))
}
impl State {
    async fn notebook_contents(
        &self,
        id: &str,
        command: crate::notebooks::files::Command,
    ) -> Result<Value> {
        let config = self.config.clone();
        let owner = format!("{}:{id}", config.instance);
        tokio::task::spawn_blocking(move || {
            client::request_timeout(
                &config.root,
                Request::ConsoleAction {
                    binding: config.binding,
                    generation: config.generation,
                    owner,
                    action: crate::console::workspace::Command::NotebookFiles { command },
                },
                Duration::from_secs(2),
            )
        })
        .await
        .map_err(|_| invalid("notebook contents bridge unavailable"))?
    }

    pub(super) async fn notebook_request(&self, id: &str, event: Transport) -> Result<Value> {
        let config = self.config.clone();
        let owner = format!("{}:{id}", config.instance);
        tokio::task::spawn_blocking(move || {
            client::request_timeout(
                &config.root,
                Request::NotebookTransport {
                    binding: config.binding,
                    generation: config.generation,
                    owner,
                    event,
                },
                Duration::from_secs(2),
            )
        })
        .await
        .map_err(|_| invalid("notebook bridge unavailable"))?
    }
    pub(super) async fn notebook_heartbeat(&self) -> Result<()> {
        let config = self.config.clone();
        let sessions = {
            let mut sessions = self.sessions.lock().unwrap();
            sessions.retain(|_, s| s.expires > Instant::now());
            sessions.keys().cloned().collect()
        };
        tokio::task::spawn_blocking(move || {
            client::request_timeout(
                &config.root,
                Request::NotebookHeartbeat {
                    binding: config.binding,
                    generation: config.generation,
                    instance: config.instance,
                    sessions,
                },
                Duration::from_secs(2),
            )
        })
        .await
        .map_err(|_| invalid("notebook owner heartbeat failed"))??;
        Ok(())
    }
    pub(super) async fn notebook_http(
        self: Arc<Self>,
        id: &str,
        request: HttpRequest<Incoming>,
    ) -> Reply {
        if request.uri().path() == "/api/notebooks/contents" {
            if request.method() != Method::POST
                || single(request.headers(), "content-type") != Some("application/json")
            {
                return fail(415, "Expected a JSON POST");
            }
            let bytes = match Limited::new(
                request.into_body(),
                crate::notebooks::files::MAX_DOCUMENT_BYTES + 4096,
            )
            .collect()
            .await
            {
                Ok(body) => body.to_bytes(),
                Err(_) => return fail(413, "Notebook document is too large"),
            };
            let command: crate::notebooks::files::Command = match serde_json::from_slice(&bytes) {
                Ok(command) => command,
                Err(_) => return fail(400, "Invalid notebook contents request"),
            };
            return match self.notebook_contents(id, command).await {
                Ok(value) => json_response(200, json!({"api_version":VERSION,"value":value})),
                Err(error) => fail(409, &error.to_string()),
            };
        }
        if request.uri().path() == "/api/notebooks/ticket" && request.method() == Method::POST {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Input {
                id: OperationId,
                generation: u64,
            }
            if single(request.headers(), "content-type") != Some("application/json") {
                return fail(415, "Expected application/json");
            }
            let bytes = match Limited::new(request.into_body(), 4096).collect().await {
                Ok(body) => body.to_bytes(),
                Err(_) => return fail(413, "Notebook ticket request too large"),
            };
            let input: Input = match serde_json::from_slice(&bytes) {
                Ok(v) => v,
                Err(_) => return fail(400, "Invalid notebook ticket request"),
            };
            if self
                .notebook_request(
                    id,
                    Transport::Check {
                        id: input.id,
                        generation: input.generation,
                    },
                )
                .await
                .is_err()
            {
                return fail(
                    409,
                    "Notebook context is unavailable or not owned by this session",
                );
            }
            let mut tickets = self.tickets.lock().unwrap();
            tickets.retain(|_, t| t.expires > Instant::now());
            if tickets.len() >= 64 {
                return fail(429, "Too many unused notebook channel tickets");
            }
            let token = match secret() {
                Ok(s) => s,
                Err(_) => return fail(500, "Channel authorization failed"),
            };
            tickets.insert(
                token.clone(),
                Ticket {
                    owner: id.into(),
                    id: input.id,
                    generation: input.generation,
                    expires: Instant::now() + Duration::from_secs(30),
                },
            );
            return json_response(
                200,
                json!({"api_version":VERSION,"protocol":PROTOCOL,"authorization_protocol":format!("sb.auth.{token}"),"expires_in_seconds":30}),
            );
        }
        let Some((kernel, generation)) = target(request.uri().path(), "/kernel") else {
            return fail(404, "Notebook route is not available");
        };
        if request.method() != Method::GET {
            return fail(405, "Use typed notebook lifecycle commands");
        }
        let access = match self
            .notebook_request(
                id,
                Transport::Connect {
                    id: kernel,
                    generation,
                },
            )
            .await
        {
            Ok(v) => v,
            Err(_) => return fail(409, "Notebook context is unavailable"),
        };
        let result = tokio::task::spawn_blocking(move || -> std::result::Result<Value, ()> {
            let port = access["port"]
                .as_u64()
                .filter(|p| *p > 0 && *p <= 65535)
                .ok_or(())?;
            let kernel: OperationId =
                serde_json::from_value(access["kernel_id"].clone()).map_err(|_| ())?;
            let token = access["token"].as_str().ok_or(())?;
            let agent: ureq::Agent = ureq::Agent::config_builder()
                .timeout_global(Some(Duration::from_secs(2)))
                .build()
                .into();
            let mut reply = agent
                .get(format!("http://127.0.0.1:{port}/api/kernels/{kernel}"))
                .header("Authorization", format!("token {token}"))
                .call()
                .map_err(|_| ())?;
            reply
                .body_mut()
                .with_config()
                .limit(65536)
                .read_json::<Value>()
                .map_err(|_| ())
        })
        .await;
        match result {
            Ok(Ok(value)) => json_response(200, value),
            _ => fail(503, "Jupyter kernel status unavailable"),
        }
    }
    pub(super) async fn notebook_ws(self: Arc<Self>, mut request: HttpRequest<Incoming>) -> Reply {
        if request.method() != Method::GET
            || single(request.headers(), "origin") != Some(self.origin.as_str())
        {
            return fail(403, "Notebook channels require the exact console origin");
        }
        let Some((kernel, generation)) = target(request.uri().path(), "/channels") else {
            return fail(404, "Notebook channel is not available");
        };
        let Some((id, _)) = self.authenticated(request.headers()) else {
            return fail(401, "Console session expired");
        };
        let Some(protocols) = single(request.headers(), "sec-websocket-protocol") else {
            return fail(403, "Notebook channel authorization required");
        };
        let protocols: Vec<_> = protocols.split(',').map(str::trim).collect();
        if protocols.len() != 2 || !protocols.contains(&PROTOCOL) {
            return fail(403, "Unsupported notebook protocol");
        }
        let Some(token) = protocols.iter().find_map(|s| s.strip_prefix("sb.auth.")) else {
            return fail(403, "Notebook channel ticket required");
        };
        let authorized = {
            let mut tickets = self.tickets.lock().unwrap();
            tickets.retain(|_, t| t.expires > Instant::now());
            if tickets
                .get(token)
                .is_some_and(|t| t.owner == id && t.id == kernel && t.generation == generation)
            {
                tickets.remove(token);
                true
            } else {
                false
            }
        };
        if !authorized {
            return fail(
                403,
                "Notebook channel ticket is invalid, expired or consumed",
            );
        }
        let permit = match self.channels.clone().try_acquire_owned() {
            Ok(p) => p,
            Err(_) => return fail(429, "Notebook channel limit reached"),
        };
        let access = match self
            .notebook_request(
                &id,
                Transport::Connect {
                    id: kernel,
                    generation,
                },
            )
            .await
        {
            Ok(v) => v,
            Err(_) => return fail(409, "Notebook context is unavailable"),
        };
        let port = match access["port"].as_u64().filter(|p| *p > 0 && *p <= 65535) {
            Some(p) => p,
            None => return fail(503, "Invalid notebook service identity"),
        };
        let upstream_id: OperationId = match serde_json::from_value(access["kernel_id"].clone()) {
            Ok(id) => id,
            Err(_) => return fail(503, "Invalid kernel identity"),
        };
        let mut upstream = match format!("ws://127.0.0.1:{port}/api/kernels/{upstream_id}/channels")
            .into_client_request()
        {
            Ok(r) => r,
            Err(_) => return fail(503, "Notebook channel unavailable"),
        };
        let Some(token) = access["token"].as_str() else {
            return fail(503, "Notebook authorization unavailable");
        };
        let token = match HeaderValue::from_str(&format!("token {token}")) {
            Ok(v) => v,
            Err(_) => return fail(503, "Notebook authorization unavailable"),
        };
        upstream.headers_mut().insert("Authorization", token);
        upstream
            .headers_mut()
            .insert("Sec-WebSocket-Protocol", HeaderValue::from_static(PROTOCOL));
        let upstream = tokio::time::timeout(
            Duration::from_secs(3),
            tokio_tungstenite::connect_async_with_config(upstream, Some(limits()), false),
        )
        .await;
        let (mut upstream, reply) = match upstream {
            Ok(Ok(pair)) => pair,
            _ => return fail(503, "Notebook channel could not connect"),
        };
        if reply
            .headers()
            .get("sec-websocket-protocol")
            .and_then(|v| v.to_str().ok())
            != Some(PROTOCOL)
        {
            return fail(503, "Jupyter protocol negotiation differed");
        }
        // HTTP 101 precedes Jupyter's asynchronous ZMQ subscription setup.
        // Tornado starts reading frames after open() completes. A protocol ping
        // therefore proves readiness before exposing the browser connection;
        // rapidly abandoned browser handshakes cannot strand half-open nudges.
        let ready = tokio::time::timeout(Duration::from_secs(3), async {
            let nonce = Bytes::from_static(b"supabricks-channel-ready");
            upstream
                .send(tungstenite::Message::Ping(nonce.clone()))
                .await
                .ok()?;
            for _ in 0..64 {
                match upstream.next().await? {
                    Ok(tungstenite::Message::Pong(value)) if value == nonce => return Some(()),
                    Ok(tungstenite::Message::Close(_)) | Err(_) => return None,
                    _ => {}
                }
            }
            None
        })
        .await;
        if !matches!(ready, Ok(Some(()))) {
            let _ = tokio::time::timeout(Duration::from_secs(1), upstream.close(None)).await;
            return fail(503, "Notebook channel did not become ready");
        }
        let cancel = {
            self.sessions
                .lock()
                .unwrap()
                .get(&id)
                .map(|s| s.cancel.subscribe())
        };
        let Some(cancel) = cancel else {
            return fail(401, "Console session expired");
        };
        let mut handshake = HttpRequest::new(());
        *handshake.method_mut() = request.method().clone();
        *handshake.uri_mut() = request.uri().clone();
        *handshake.version_mut() = request.version();
        *handshake.headers_mut() = request.headers().clone();
        let mut reply = match tungstenite::handshake::server::create_response(&handshake) {
            Ok(r) => r.map(|_| Full::new(Bytes::new())),
            Err(_) => return fail(400, "Invalid WebSocket handshake"),
        };
        reply
            .headers_mut()
            .insert("Sec-WebSocket-Protocol", HeaderValue::from_static(PROTOCOL));
        reply
            .headers_mut()
            .insert("Cache-Control", HeaderValue::from_static("no-store"));
        let upgrade = hyper::upgrade::on(&mut request);
        tokio::spawn(async move {
            let _permit = permit;
            if let Ok(Ok(stream)) = tokio::time::timeout(Duration::from_secs(3), upgrade).await {
                let browser = WebSocketStream::from_raw_socket(
                    TokioIo::new(stream),
                    Role::Server,
                    Some(limits()),
                )
                .await;
                self.relay(id, kernel, generation, cancel, browser, upstream)
                    .await;
            }
        });
        reply
    }
    async fn relay<B, U>(
        self: Arc<Self>,
        id: String,
        kernel: OperationId,
        generation: u64,
        mut cancel: watch::Receiver<bool>,
        mut browser: WebSocketStream<B>,
        mut upstream: WebSocketStream<U>,
    ) where
        B: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
        U: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
    {
        let mut check = tokio::time::interval(Duration::from_millis(500));
        loop {
            tokio::select! {
                biased;
                _=cancel.changed()=>break,
                _=check.tick()=>{
                    let live=self.sessions.lock().unwrap().get(&id).is_some_and(|s|s.expires>Instant::now());
                    if !live || self.notebook_request(&id,Transport::Check{id:kernel,generation}).await.is_err(){break;}
                }
                item=browser.next()=>{
                    let Some(Ok(message))=item else{break};
                    if message.is_close(){break;}
                    if !matches!(message,tungstenite::Message::Binary(_)|tungstenite::Message::Ping(_)|tungstenite::Message::Pong(_)){break;}
                    if !matches!(tokio::time::timeout(Duration::from_secs(1),upstream.send(message)).await,Ok(Ok(()))){break;}
                }
                item=upstream.next()=>{
                    let Some(Ok(message))=item else{break};
                    if message.is_close(){break;}
                    if !matches!(tokio::time::timeout(Duration::from_secs(1),browser.send(message)).await,Ok(Ok(()))){break;}
                }
            }
        }
        let _ = tokio::time::timeout(Duration::from_secs(1), browser.close(None)).await;
        let _ = tokio::time::timeout(Duration::from_secs(1), upstream.close(None)).await;
    }
}
