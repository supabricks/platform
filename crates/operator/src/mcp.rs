//! The MCP façade (RFC 012 D8, 004 B3): streamable-HTTP JSON-RPC served from
//! the operator binary. Hand-rolled per D8's sanctioned fallback. POST carries
//! JSON-RPC (202 for notifications); GET holds a keep-alive-only SSE stream —
//! no session model or server notifications, just enough for clients
//! that treat a missing stream as a dead server. Every tool is a thin
//! verb over the CR model; the reconcilers stay the single implementation of
//! behavior (001 §5: one machine API, many clients).
//!
//! Error contract, three layers (review 003 P1-5):
//! 1. HTTP: 401 unauthorized (bearer mode only); 400 + JSON-RPC parse-error
//!    envelope (code -32700) for unparseable bodies.
//! 2. JSON-RPC: `error` envelope (e.g. -32601 unknown method), HTTP 200.
//! 3. Tool: `result.isError=true`, content = {reason, retriable,
//!    suggested_action} — the layer agents act on.

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use kube::ResourceExt;
use kube::api::{Api, DeleteParams, ListParams, Patch, PatchParams};
use serde_json::{Value, json};
use tracing::info;

use crate::crd::{Branch, Database, EnrolledDatabase, Phase};
use crate::reconcile::{Ctx, WAKE_ANNOTATION, now_ts};

pub struct McpState {
    pub ctx: Arc<Ctx>,
    /// None = open mode (POC default: ports bind loopback, the network layer
    /// is the guard; real IAM lands per RFC 008). Some = require this bearer.
    pub token: Option<String>,
    pub connect_host: String,
}

/// The UI bundle, embedded at compile time (RFC 013): served from the same
/// origin as /mcp — no CORS, no extra pod, air-gap clean.
#[derive(rust_embed::RustEmbed)]
#[folder = "../../ui/dist"]
struct UiAssets;

async fn serve_ui(uri: axum::http::Uri) -> Response {
    let path = uri.path().trim_start_matches('/');
    let path = if path.is_empty() { "index.html" } else { path };
    match UiAssets::get(path) {
        Some(f) => {
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            (
                [(axum::http::header::CONTENT_TYPE, mime.to_string())],
                f.data.into_owned(),
            )
                .into_response()
        }
        None => (StatusCode::NOT_FOUND, "not found").into_response(),
    }
}

/// Streamable-HTTP GET leg: an open, idle server→client stream. Claude Code
/// tolerates a 405 here; other MCP clients may treat a
/// missing stream as a dead server — so we hold one open with keep-alives.
async fn handle_get() -> Response {
    let stream =
        futures::stream::pending::<Result<axum::response::sse::Event, std::convert::Infallible>>();
    axum::response::sse::Sse::new(stream)
        .keep_alive(
            axum::response::sse::KeepAlive::new().interval(std::time::Duration::from_secs(15)),
        )
        .into_response()
}

pub async fn serve(state: Arc<McpState>, addr: &str) -> anyhow::Result<()> {
    let app = Router::new()
        .route("/mcp", post(handle_post).get(handle_get))
        .fallback(axum::routing::get(serve_ui))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!("MCP server + UI listening on {addr}");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn handle_post(
    State(state): State<Arc<McpState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let authed = match &state.token {
        None => true,
        Some(t) => headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v == format!("Bearer {t}")),
    };
    if !authed {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "invalid authorization token"})),
        )
            .into_response();
    }
    let Ok(req) = serde_json::from_slice::<Value>(&body) else {
        // JSON-RPC parse-error envelope (review 003 P1-5), not an ad-hoc shape.
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"jsonrpc": "2.0", "id": null,
                         "error": {"code": -32700, "message": "parse error: body is not valid JSON"}})),
        )
            .into_response();
    };
    let method = req["method"].as_str().unwrap_or_default().to_string();
    let id = req["id"].clone();
    if id.is_null() {
        // Notification (e.g. notifications/initialized): acknowledge, no body.
        return StatusCode::ACCEPTED.into_response();
    }
    let result = match method.as_str() {
        "initialize" => Ok(json!({
            "protocolVersion": req["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sspc", "version": env!("CARGO_PKG_VERSION")},
        })),
        "tools/list" => Ok(json!({"tools": tool_defs()})),
        "tools/call" => {
            let name = req["params"]["name"].as_str().unwrap_or_default();
            let args = req["params"]["arguments"].clone();
            match call_tool(&state, name, &args).await {
                Ok(v) => Ok(json!({
                    "content": [{"type": "text", "text": v.to_string()}],
                    "isError": false,
                })),
                Err(e) => Ok(json!({
                    "content": [{"type": "text", "text": e.to_string()}],
                    "isError": true,
                })),
            }
        }
        _ => Err(json!({"code": -32601, "message": format!("unknown method {method}")})),
    };
    let reply = match result {
        Ok(r) => json!({"jsonrpc": "2.0", "id": id, "result": r}),
        Err(e) => json!({"jsonrpc": "2.0", "id": id, "error": e}),
    };
    Json(reply).into_response()
}

/// Structured, remediable errors (001 §5.2): agents act on failures.
#[derive(Debug)]
struct ToolError {
    reason: String,
    retriable: bool,
    suggested_action: String,
}

impl std::fmt::Display for ToolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}",
            json!({"reason": self.reason, "retriable": self.retriable,
                   "suggested_action": self.suggested_action})
        )
    }
}

fn terr(reason: impl Into<String>, retriable: bool, action: impl Into<String>) -> ToolError {
    ToolError {
        reason: reason.into(),
        retriable,
        suggested_action: action.into(),
    }
}

fn from_kube(e: kube::Error) -> ToolError {
    terr(
        format!("kubernetes api error: {e}"),
        true,
        "retry; if it persists, check operator logs",
    )
}

type ToolResult = Result<Value, ToolError>;

async fn call_tool(state: &McpState, name: &str, args: &Value) -> ToolResult {
    match name {
        "capabilities" => capabilities(state),
        "create_database" => create_database(state, args).await,
        "list_databases" => list_databases(state).await,
        "get_database" => get_database(state, args).await,
        "delete_database" => delete_database(state, args).await,
        "create_branch" => create_branch(state, args).await,
        "list_branches" => list_branches(state).await,
        "delete_branch" => delete_branch(state, args).await,
        "get_connection" => get_connection(state, args).await,
        "enroll_database" => enroll_database(state, args).await,
        "unenroll_database" => unenroll_database(state, args).await,
        "get_events" => get_events(state).await,
        "get_metrics" => get_metrics(state, args).await,
        "get_cu_ledger" => get_cu_ledger(state).await,
        other => Err(terr(
            format!("unknown tool {other}"),
            false,
            "call tools/list for the available tools",
        )),
    }
}

fn from_validation(error: supabricks_core::error::ValidationError) -> ToolError {
    terr(error.message, false, error.hint)
}

fn valid_name(args: &Value, key: &str) -> Result<String, ToolError> {
    supabricks_core::validation::valid_name(args, key).map_err(from_validation)
}

fn need_name(args: &Value) -> Result<String, ToolError> {
    valid_name(args, "name")
}

/// Endpoint capacity pre-check: the M1 NodePort block is finite, and
/// exhaustion discovered inside the reconciler surfaces only as retry logs —
/// refuse synchronously with a structured error instead. Idempotent creates
/// of an EXISTING endpoint (its Service already holds a port) always pass.
async fn ensure_capacity(state: &McpState, name: &str) -> Result<(), ToolError> {
    let svcs: Api<k8s_openapi::api::core::v1::Service> =
        Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let list = svcs
        .list(&ListParams::default().labels(crate::reconcile::ENDPOINT_LABEL))
        .await
        .map_err(from_kube)?;
    if list.items.iter().any(|s| s.name_any() == name) {
        return Ok(());
    }
    if list.items.len() as i32 >= crate::ports::RANGE_LEN {
        return Err(terr(
            format!(
                "endpoint capacity reached ({} NodePorts in the M1 block)",
                crate::ports::RANGE_LEN
            ),
            false,
            "delete an endpoint to free its port (suspended endpoints keep theirs); the M2 gateway removes this cap",
        ));
    }
    Ok(())
}

fn bounded_int(args: &Value, key: &str, min: i64, max: i64) -> Result<Option<i64>, ToolError> {
    supabricks_core::validation::bounded_int(args, key, min, max).map_err(from_validation)
}

fn parse_priority(args: &Value) -> Result<Option<&'static str>, ToolError> {
    supabricks_core::validation::parse_priority(args).map_err(from_validation)
}

fn capabilities(state: &McpState) -> ToolResult {
    Ok(json!({
        "platform": "sspc", "version": env!("CARGO_PKG_VERSION"),
        "pg_version": u16::from(state.ctx.storcon.pg_major()),
        "max_endpoints": crate::ports::RANGE_LEN,
        "connect_host": state.connect_host,
        "features": {"branching": true, "branch_at_time": true, "branch_of_branch": true,
                      "per_database_credentials": true,
                      "ttl": true, "scale_to_zero": true,
                      "enrollment": "attach existing Postgres for inventory/health, zero migration",
                      "wake": "explicit via get_connection (plain-psql wake-on-connect: M2)"},
    }))
}

/// Connection URI with the endpoint's own credential (RFC 014 H3). An
/// unreadable credential is a structured, retriable error (review 001 P1-2)
/// — never a URI with a guessed password.
async fn uri(state: &McpState, name: &str, port: i32) -> Result<String, ToolError> {
    let pw = crate::reconcile::endpoint_password(&state.ctx, name)
        .await
        .map_err(|e| {
            terr(
                format!("credential unavailable: {e:#}"),
                true,
                "retry shortly; if it persists, check the operator's Secret RBAC and logs",
            )
        })?;
    Ok(format!(
        "postgresql://cloud_admin:{pw}@{}:{port}/postgres",
        state.connect_host
    ))
}

/// Wait until the CR has a port and its compute pod is Ready. Bounded; on
/// timeout we return state honestly rather than hang (001 §5.2 "predictably
/// async" — M1's cheap version).
async fn await_ready(state: &McpState, name: &str, secs: u64) -> Option<i32> {
    let ns = &state.ctx.namespace;
    let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(state.ctx.client.clone(), ns);
    let dbs: Api<Database> = Api::namespaced(state.ctx.client.clone(), ns);
    let brs: Api<Branch> = Api::namespaced(state.ctx.client.clone(), ns);
    let deadline = Instant::now() + Duration::from_secs(secs);
    loop {
        let port = if let Ok(Some(db)) = dbs.get_opt(name).await {
            db.status.as_ref().and_then(|s| s.node_port)
        } else if let Ok(Some(br)) = brs.get_opt(name).await {
            br.status.as_ref().and_then(|s| s.node_port)
        } else {
            None
        };
        if let Some(p) = port {
            let ready = pods.get_opt(name).await.ok().flatten().is_some_and(|pod| {
                pod.status
                    .and_then(|s| s.conditions)
                    .unwrap_or_default()
                    .iter()
                    .any(|c| c.type_ == "Ready" && c.status == "True")
            });
            if ready {
                return Some(p);
            }
        }
        if Instant::now() > deadline {
            return None;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

async fn create_database(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    ensure_capacity(state, &name).await?;
    // Create-during-delete returns success against the DYING endpoint (the
    // SSA lands as an update on the deleting CR and the old pod still reads
    // Ready) — found by the idempotency torture e2e. Refuse retriably.
    let pre: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    if let Some(existing) = pre.get_opt(&name).await.map_err(from_kube)? {
        if existing.metadata.deletion_timestamp.is_some() {
            return Err(terr(
                format!("{name} is still being deleted"),
                true,
                "retry in a few seconds, once the previous instance finishes deleting",
            ));
        }
    }
    let mut spec = json!({});
    if let Some(ttl) = bounded_int(args, "ttl_seconds", 1, 30 * 86400)? {
        spec["ttlSeconds"] = json!(ttl);
    }
    // 0 = never suspend (the lifecycle loop skips non-positive values) —
    // documented contract, review 003 P1-2.
    if let Some(s) = bounded_int(args, "suspend_after_seconds", 0, 86400)? {
        spec["suspendAfterSeconds"] = json!(s);
    }
    if let Some(c) = bounded_int(args, "cu_limit", 1, 960)? {
        spec["cuLimit"] = json!(c);
    }
    if let Some(p) = parse_priority(args)? {
        spec["priority"] = json!(p);
    }
    let db: Database = serde_json::from_value(json!({
        "apiVersion": "sspc.io/v1alpha1", "kind": "Database",
        "metadata": {"name": name, "namespace": state.ctx.namespace},
        "spec": spec,
    }))
    .map_err(|e| terr(format!("bad arguments: {e}"), false, "check argument types"))?;
    let api: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    // Server-side apply on the name = natural idempotency: retries converge.
    api.patch(&name, &PatchParams::apply("sspc-mcp"), &Patch::Apply(&db))
        .await
        .map_err(from_kube)?;
    match await_ready(state, &name, 30).await {
        Some(port) => Ok(json!({
            "name": name, "status": "ready", "connection_uri": uri(state, &name, port).await?,
            "note": "psql-ready now; TTL reaping and scale-to-zero arrive in P3",
        })),
        None => Ok(json!({
            "name": name, "status": "provisioning",
            "note": "not ready within 30s; call get_connection to poll",
        })),
    }
}

async fn create_branch(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    // Review 003 P1-1: validate the owning database's NAME and EXISTENCE up
    // front — a typo must be a synchronous structured error, not a Branch CR
    // stuck in permanent retry.
    let database = valid_name(args, "database")?;
    ensure_capacity(state, &name).await?;
    let pre: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    if let Some(existing) = pre.get_opt(&name).await.map_err(from_kube)? {
        if existing.metadata.deletion_timestamp.is_some() {
            return Err(terr(
                format!("{name} is still being deleted"),
                true,
                "retry in a few seconds, once the previous instance finishes deleting",
            ));
        }
    }
    let dbs: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    if dbs.get_opt(&database).await.map_err(from_kube)?.is_none() {
        return Err(terr(
            format!("database {database} not found"),
            false,
            "list_databases to see what exists; create_database to make it",
        ));
    }
    let mut spec = json!({"database": database});
    // Branch-of-branch (RFC 014 H2): validate up front so the error is
    // synchronous and structured, not a reconciler retry loop.
    if let Some(p) = args["parent"].as_str() {
        let brs: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
        match brs.get_opt(p).await.map_err(from_kube)? {
            Some(pbr) if pbr.spec.database == database => {}
            Some(pbr) => {
                return Err(terr(
                    format!(
                        "parent branch {p} belongs to database {}, not {database}",
                        pbr.spec.database
                    ),
                    false,
                    "pass the database that owns the parent branch",
                ));
            }
            None => {
                return Err(terr(
                    format!("parent branch {p} not found"),
                    false,
                    "list_branches to see what exists",
                ));
            }
        }
        spec["parent"] = json!(p);
    }
    // Branch point (RFC 014 H2): LSN or RFC 3339 timestamp; default head-now.
    if let Some(a) = args["at"].as_str() {
        spec["at"] = json!(a);
    }
    if let Some(ttl) = bounded_int(args, "ttl_seconds", 1, 30 * 86400)? {
        spec["ttlSeconds"] = json!(ttl);
    }
    if let Some(c) = bounded_int(args, "cu_limit", 1, 960)? {
        spec["cuLimit"] = json!(c);
    }
    if let Some(p) = parse_priority(args)? {
        spec["priority"] = json!(p);
    }
    let br: Branch = serde_json::from_value(json!({
        "apiVersion": "sspc.io/v1alpha1", "kind": "Branch",
        "metadata": {"name": name, "namespace": state.ctx.namespace},
        "spec": spec,
    }))
    .map_err(|e| terr(format!("bad arguments: {e}"), false, "check argument types"))?;
    let api: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    api.patch(&name, &PatchParams::apply("sspc-mcp"), &Patch::Apply(&br))
        .await
        .map_err(from_kube)?;
    match await_ready(state, &name, 30).await {
        Some(port) => Ok(json!({
            "name": name, "parent": args["parent"].as_str().unwrap_or(&database),
            "database": database, "status": "ready",
            "connection_uri": uri(state, &name, port).await?,
        })),
        None => {
            // Distinguish "slow" from "failed" (a bad `at` fails the CR).
            if let Ok(Some(br)) = api.get_opt(&name).await {
                if let Some(s) = br.status.filter(|s| s.phase == Some(Phase::Failed)) {
                    return Err(terr(
                        s.message.unwrap_or_else(|| "branch failed".into()),
                        false,
                        "fix the branch point (`at`) and recreate; delete_branch removes this one",
                    ));
                }
            }
            Ok(json!({"name": name, "status": "provisioning",
                       "note": "call get_connection to poll"}))
        }
    }
}

fn summarize(status: Option<&crate::crd::EndpointStatus>) -> Value {
    match status {
        Some(s) => json!({"phase": s.phase, "node_port": s.node_port,
                           "tenant_id": s.tenant_id, "timeline_id": s.timeline_id,
                           "last_activity": s.last_activity, "suspended_at": s.suspended_at}),
        None => json!({"phase": null}),
    }
}

async fn list_databases(state: &McpState) -> ToolResult {
    let api: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let items = api.list(&Default::default()).await.map_err(from_kube)?;
    let mut out: Vec<Value> = items
        .items
        .iter()
        .map(|d| {
            let mut v = summarize(d.status.as_ref());
            v["name"] = json!(d.name_any());
            v["kind"] = json!("cell-backed");
            v["created_at"] = json!(
                d.metadata
                    .creation_timestamp
                    .as_ref()
                    .map(|t| t.0.to_string())
            );
            v["ttl_seconds"] = json!(d.spec.ttl_seconds);
            v["suspend_after_seconds"] = json!(d.spec.suspend_after_seconds);
            v
        })
        .collect();
    // The estate view includes enrolled (foreign) Postgres — RFC 010.
    let edbs: Api<EnrolledDatabase> =
        Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    for e in edbs
        .list(&Default::default())
        .await
        .map_err(from_kube)?
        .items
    {
        let s = e.status.clone().unwrap_or_default();
        out.push(json!({
            "name": e.name_any(), "kind": "enrolled",
            "phase": s.phase, "server_version": s.server_version,
            "database_count": s.database_count, "total_size": s.total_size,
            "last_checked": s.last_checked, "message": s.message,
        }));
    }
    Ok(json!(out))
}

async fn enroll_database(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    let uri_arg = args["connection_uri"].as_str().ok_or_else(|| {
        terr(
            "missing required argument: connection_uri",
            false,
            "pass a postgres:// connection URI (a read-only monitoring role is enough)",
        )
    })?;
    let edb: EnrolledDatabase = serde_json::from_value(json!({
        "apiVersion": "sspc.io/v1alpha1", "kind": "EnrolledDatabase",
        "metadata": {"name": name, "namespace": state.ctx.namespace},
        "spec": {"connectionUri": uri_arg},
    }))
    .map_err(|e| terr(format!("bad arguments: {e}"), false, "check argument types"))?;
    let api: Api<EnrolledDatabase> =
        Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    api.patch(&name, &PatchParams::apply("sspc-mcp"), &Patch::Apply(&edb))
        .await
        .map_err(from_kube)?;
    // First health check lands on the next lifecycle tick (≤15s); wait briefly.
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if let Ok(Some(e)) = api.get_opt(&name).await {
            if let Some(s) = e.status.filter(|s| s.phase.is_some()) {
                return Ok(json!({
                    "name": name, "kind": "enrolled", "phase": s.phase,
                    "server_version": s.server_version,
                    "database_count": s.database_count, "total_size": s.total_size,
                    "message": s.message,
                    "note": "enrolled: inventoried and health-monitored in place; nothing was migrated or modified",
                }));
            }
        }
        if Instant::now() > deadline {
            return Ok(json!({"name": name, "kind": "enrolled", "phase": "pending",
                              "note": "first health check pending; list_databases will show it shortly"}));
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

async fn unenroll_database(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    let api: Api<EnrolledDatabase> =
        Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    match api.delete(&name, &kube::api::DeleteParams::default()).await {
        Ok(_) => Ok(json!({"name": name, "status": "unenrolled",
                            "note": "the database itself was never touched"})),
        Err(kube::Error::Api(e)) if e.code == 404 => Ok(json!({"name": name, "status": "absent"})),
        Err(e) => Err(from_kube(e)),
    }
}

async fn list_branches(state: &McpState) -> ToolResult {
    let api: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let items = api.list(&Default::default()).await.map_err(from_kube)?;
    Ok(json!(
        items
            .items
            .iter()
            .map(|b| {
                let mut v = summarize(b.status.as_ref());
                v["name"] = json!(b.name_any());
                v["database"] = json!(b.spec.database);
                v["parent"] = json!(b.spec.parent);
                v["at"] = json!(b.spec.at);
                v["created_at"] = json!(
                    b.metadata
                        .creation_timestamp
                        .as_ref()
                        .map(|t| t.0.to_string())
                );
                v["ttl_seconds"] = json!(b.spec.ttl_seconds);
                v
            })
            .collect::<Vec<_>>()
    ))
}

async fn get_database(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    let api: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    match api.get_opt(&name).await.map_err(from_kube)? {
        Some(d) => {
            let mut v = summarize(d.status.as_ref());
            v["name"] = json!(name);
            Ok(v)
        }
        None => Err(terr(
            format!("database {name} not found"),
            false,
            "list_databases to see what exists, or create_database",
        )),
    }
}

async fn get_connection(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    let ns = &state.ctx.namespace;
    let dbs: Api<Database> = Api::namespaced(state.ctx.client.clone(), ns);
    let brs: Api<Branch> = Api::namespaced(state.ctx.client.clone(), ns);

    // The wake path (RFC 012 P3): a suspended endpoint gets a wake annotation;
    // the reconciler recreates the compute; we wait and report the wake time.
    let wake_patch = Patch::Merge(json!({
        "metadata": {"annotations": {WAKE_ANNOTATION: now_ts()}}
    }));
    let suspended = |s: &Option<crate::crd::EndpointStatus>| {
        s.as_ref().map(|s| s.phase) == Some(Some(Phase::Suspended))
    };
    let mut woke = false;
    if let Some(db) = dbs.get_opt(&name).await.map_err(from_kube)? {
        if suspended(&db.status) {
            dbs.patch(&name, &PatchParams::default(), &wake_patch)
                .await
                .map_err(from_kube)?;
            woke = true;
        }
    } else if let Some(br) = brs.get_opt(&name).await.map_err(from_kube)? {
        if suspended(&br.status) {
            brs.patch(&name, &PatchParams::default(), &wake_patch)
                .await
                .map_err(from_kube)?;
            woke = true;
        }
    } else {
        return Err(terr(
            format!("{name} not found"),
            false,
            "list_databases / list_branches to see what exists",
        ));
    }

    let t0 = Instant::now();
    match await_ready(state, &name, 45).await {
        Some(port) => {
            let mut out = json!({"name": name, "connection_uri": uri(state, &name, port).await?});
            if woke {
                out["woke_from_suspend"] = json!(true);
                out["wake_seconds"] = json!(format!("{:.1}", t0.elapsed().as_secs_f64()));
            }
            Ok(out)
        }
        None => Err(terr(
            format!("{name} is not ready"),
            true,
            "retry shortly; if it stays unready, check the operator logs",
        )),
    }
}

async fn delete_database(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    // RFC 014 H1: the tenant delete would destroy every branch's storage and
    // orphan their CRs — refuse, naming the children, instead of cascading.
    let brs: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let children: Vec<String> = brs
        .list(&Default::default())
        .await
        .map_err(from_kube)?
        .items
        .iter()
        .filter(|b| b.spec.database == name && b.metadata.deletion_timestamp.is_none())
        .map(|b| b.name_any())
        .collect();
    if !children.is_empty() {
        return Err(terr(
            format!(
                "database {name} has {} live branch(es): {}",
                children.len(),
                children.join(", ")
            ),
            false,
            "delete those branches first (delete_branch), then delete the database",
        ));
    }
    let api: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    match api.delete(&name, &DeleteParams::default()).await {
        Ok(_) => Ok(json!({"name": name, "status": "deleting",
                            "note": "storage and compute are reclaimed by the operator"})),
        Err(kube::Error::Api(e)) if e.code == 404 => Ok(json!({"name": name, "status": "absent"})),
        Err(e) => Err(from_kube(e)),
    }
}

async fn delete_branch(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    let api: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    // H1, one level down: this branch may itself be a parent (H2).
    let children: Vec<String> = api
        .list(&Default::default())
        .await
        .map_err(from_kube)?
        .items
        .iter()
        .filter(|b| {
            b.spec.parent.as_deref() == Some(name.as_str())
                && b.metadata.deletion_timestamp.is_none()
        })
        .map(|b| b.name_any())
        .collect();
    if !children.is_empty() {
        return Err(terr(
            format!(
                "branch {name} has {} child branch(es): {}",
                children.len(),
                children.join(", ")
            ),
            false,
            "delete those child branches first, then this one",
        ));
    }
    match api.delete(&name, &DeleteParams::default()).await {
        Ok(_) => Ok(json!({"name": name, "status": "deleting"})),
        Err(kube::Error::Api(e)) if e.code == 404 => Ok(json!({"name": name, "status": "absent"})),
        Err(e) => Err(from_kube(e)),
    }
}

/// Recent lifecycle events — the audit line (013 ticker; agents get it too).
async fn get_events(state: &McpState) -> ToolResult {
    let api: Api<k8s_openapi::api::core::v1::Event> =
        Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let mut evs: Vec<_> = api
        .list(&Default::default())
        .await
        .map_err(from_kube)?
        .items
        .into_iter()
        .filter(|e| {
            matches!(
                e.involved_object.kind.as_deref(),
                Some("Database") | Some("Branch") | Some("EnrolledDatabase")
            )
        })
        .map(|e| {
            let t = e
                .last_timestamp
                .as_ref()
                .map(|t| t.0.to_string())
                .or_else(|| {
                    e.metadata
                        .creation_timestamp
                        .as_ref()
                        .map(|t| t.0.to_string())
                });
            json!({
                "time": t,
                "reason": e.reason,
                "kind": e.involved_object.kind,
                "name": e.involved_object.name,
                "message": e.message,
            })
        })
        .collect();
    evs.sort_by(|a, b| b["time"].as_str().cmp(&a["time"].as_str()));
    evs.truncate(30);
    Ok(json!(evs))
}

/// Basic usage series for one endpoint (013 round 2; 001 §5.4 toolset).
async fn get_metrics(state: &McpState, args: &Value) -> ToolResult {
    let name = need_name(args)?;
    let series: Vec<Value> = {
        let m = state.ctx.metrics.lock().unwrap();
        m.get(&name)
            .map(|ring| {
                ring.iter()
                    .map(|(t, cpu, mem)| json!({"t": t, "cpu_millis": cpu, "mem_mib": mem}))
                    .collect()
            })
            .unwrap_or_default()
    };
    let dbs: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let brs: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let (cu, prio) = if let Ok(Some(d)) = dbs.get_opt(&name).await {
        (d.spec.cu_limit, format!("{:?}", d.spec.priority))
    } else if let Ok(Some(b)) = brs.get_opt(&name).await {
        (b.spec.cu_limit, format!("{:?}", b.spec.priority))
    } else {
        return Err(terr(
            format!("{name} not found"),
            false,
            "list_databases to see what exists",
        ));
    };
    Ok(json!({"name": name, "cu_limit": cu, "priority": prio,
               "cpu_limit_millis": cu * 100, "series": series}))
}

/// Kubernetes CPU quantities come as "8", "0.5", "7910m", or (rarely) "…n".
fn cpu_quantity_millis(q: &str) -> i64 {
    if let Some(n) = q.strip_suffix('m') {
        n.parse().unwrap_or(0)
    } else if let Some(n) = q.strip_suffix('n') {
        n.parse::<i64>().unwrap_or(0) / 1_000_000
    } else {
        (q.parse::<f64>().unwrap_or(0.0) * 1000.0) as i64
    }
}

/// The oversubscription ledger (RFC 011): what the cluster physically has vs
/// what has been promised as CU ceilings vs what is awake and drawing now.
/// Suspended endpoints hold zero CU — that gap is the whole business model.
async fn get_cu_ledger(state: &McpState) -> ToolResult {
    let nodes: Api<k8s_openapi::api::core::v1::Node> = Api::all(state.ctx.client.clone());
    let mut physical_millis: i64 = 0;
    for n in nodes
        .list(&Default::default())
        .await
        .map_err(from_kube)?
        .items
    {
        if let Some(q) = n
            .status
            .as_ref()
            .and_then(|s| s.allocatable.as_ref())
            .and_then(|a| a.get("cpu"))
        {
            physical_millis += cpu_quantity_millis(&q.0);
        }
    }

    let dbs: Api<Database> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let brs: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let mut promised: i64 = 0;
    let mut active: i64 = 0;
    let mut active_names: Vec<String> = Vec::new();
    let mut endpoints: i64 = 0;
    let mut tally = |name: String, cu: i64, phase: Option<Phase>| {
        endpoints += 1;
        promised += cu;
        if phase == Some(Phase::Active) {
            active += cu;
            active_names.push(name);
        }
    };
    for d in dbs
        .list(&Default::default())
        .await
        .map_err(from_kube)?
        .items
    {
        tally(
            d.name_any(),
            d.spec.cu_limit,
            d.status.as_ref().and_then(|s| s.phase),
        );
    }
    for b in brs
        .list(&Default::default())
        .await
        .map_err(from_kube)?
        .items
    {
        tally(
            b.name_any(),
            b.spec.cu_limit,
            b.status.as_ref().and_then(|s| s.phase),
        );
    }

    let used_millis: i64 = {
        let m = state.ctx.metrics.lock().unwrap();
        active_names
            .iter()
            .filter_map(|n| {
                m.get(n)
                    .and_then(|ring| ring.back())
                    .map(|(_, cpu, _)| *cpu)
            })
            .sum()
    };

    Ok(json!({
        "physical_cu": physical_millis / 100,
        "promised_cu": promised,
        "active_cu": active,
        "used_millis": used_millis,
        "endpoints": endpoints,
        "endpoints_active": active_names.len(),
    }))
}

fn tool_defs() -> Value {
    let name_arg = json!({"type": "string", "description": "Resource name; normalized to lowercase (the canonical name is echoed in every response)"});
    // Result contracts (review 003 P1-4): every tool declares an
    // outputSchema. These live in the same snapshot fixture as the input
    // schemas, so result-field drift fails CI before it reaches a client.
    let err_note =
        "On failure the content is {reason, retriable, suggested_action} with isError=true.";
    let estate_row = json!({"type": "object", "properties": {
        "name": {"type": "string"}, "kind": {"type": "string", "enum": ["cell-backed", "enrolled"]},
        "phase": {"type": ["string", "null"]}, "node_port": {"type": ["integer", "null"]},
        "tenant_id": {"type": ["string", "null"]}, "timeline_id": {"type": ["string", "null"]},
        "last_activity": {"type": ["string", "null"]}, "suspended_at": {"type": ["string", "null"]},
        "created_at": {"type": ["string", "null"]}, "ttl_seconds": {"type": ["integer", "null"]},
        "suspend_after_seconds": {"type": ["integer", "null"]},
        "server_version": {"type": ["string", "null"]}, "database_count": {"type": ["integer", "null"]},
        "total_size": {"type": ["string", "null"]}, "message": {"type": ["string", "null"]}},
        "required": ["name"]});
    let create_result = json!({"type": "object", "description": err_note, "properties": {
        "name": {"type": "string"},
        "status": {"type": "string", "enum": ["ready", "provisioning"]},
        "connection_uri": {"type": "string", "description": "present when status=ready"},
        "note": {"type": "string"}},
        "required": ["name", "status"]});
    json!([
        {"name": "capabilities",
         "description": "Platform capabilities, limits, and feature flags.",
         "inputSchema": {"type": "object", "properties": {}},
         "outputSchema": {"type": "object", "properties": {
             "platform": {"type": "string"}, "version": {"type": "string"},
             "pg_version": {"type": "integer"}, "max_endpoints": {"type": "integer"},
             "connect_host": {"type": "string"}, "features": {"type": "object"}},
             "required": ["platform", "features"]}},
        {"name": "create_database",
         "description": "Create a Postgres database; returns a psql connection URI. Idempotent on name.",
         "inputSchema": {"type": "object", "properties": {
             "name": name_arg,
             "ttl_seconds": {"type": "integer", "description": "Optional TTL; the platform reaps the database when it expires"},
             "suspend_after_seconds": {"type": "integer", "description": "Idle seconds before scale-to-zero (default 300)"},
             "cu_limit": {"type": "integer", "description": "Compute ceiling in CU, 1 CU = 0.1 core (default 10)"},
             "priority": {"type": "string", "enum": ["high", "standard", "low"], "description": "Contention priority: high degrades last, low is preempted first (default standard)"}},
             "required": ["name"]},
         "outputSchema": create_result},
        {"name": "list_databases",
         "description": "The estate: cell-backed databases AND enrolled (existing, unmigrated) Postgres, with health.",
         "inputSchema": {"type": "object", "properties": {}},
         "outputSchema": {"type": "array", "items": estate_row}},
        {"name": "enroll_database",
         "description": "Attach an EXISTING Postgres (anywhere) for inventory and health monitoring — zero migration, zero changes to it. Needs only a connection URI (read-only role is enough).",
         "inputSchema": {"type": "object", "properties": {
             "name": name_arg,
             "connection_uri": {"type": "string", "description": "postgres:// URI of the existing server"}},
             "required": ["name", "connection_uri"]},
         "outputSchema": {"type": "object", "description": err_note, "properties": {
             "name": {"type": "string"}, "kind": {"type": "string"}, "phase": {"type": ["string", "null"]},
             "server_version": {"type": ["string", "null"]}, "database_count": {"type": ["integer", "null"]},
             "total_size": {"type": ["string", "null"]}, "message": {"type": ["string", "null"]},
             "note": {"type": "string"}}, "required": ["name"]}},
        {"name": "unenroll_database",
         "description": "Detach an enrolled database from the estate (the database itself is never touched).",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]},
         "outputSchema": {"type": "object", "properties": {
             "name": {"type": "string"}, "status": {"type": "string", "enum": ["unenrolled", "absent"]},
             "note": {"type": "string"}}, "required": ["name", "status"]}},
        {"name": "get_database", "description": "Get one database's status.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]},
         "outputSchema": estate_row},
        {"name": "delete_database",
         "description": "Delete a database, its storage, and its compute. Refuses (with the list) while the database still has branches.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]},
         "outputSchema": {"type": "object", "description": err_note, "properties": {
             "name": {"type": "string"}, "status": {"type": "string", "enum": ["deleting", "absent"]},
             "note": {"type": "string"}}, "required": ["name", "status"]}},
        {"name": "create_branch",
         "description": "Instant copy-on-write branch; returns its own connection URI. Branch a database, another branch (parent), and/or a moment in time (at).",
         "inputSchema": {"type": "object", "properties": {
             "name": name_arg,
             "database": {"type": "string", "description": "Owning database name (the root of the branch tree)"},
             "parent": {"type": "string", "description": "Optional parent BRANCH name — branch-of-branch; default branches the database itself"},
             "at": {"type": "string", "description": "Optional branch point: an LSN (e.g. 0/1BCC200) or RFC 3339 timestamp (e.g. 2026-08-12T10:00:00Z); default = head of parent now"},
             "ttl_seconds": {"type": "integer", "description": "Optional TTL"},
             "cu_limit": {"type": "integer", "description": "Compute ceiling in CU (default 10)"},
             "priority": {"type": "string", "enum": ["high", "standard", "low"]}},
             "required": ["name", "database"]},
         "outputSchema": create_result},
        {"name": "list_branches", "description": "List branches with status and parentage.",
         "inputSchema": {"type": "object", "properties": {}},
         "outputSchema": {"type": "array", "items": {"type": "object", "properties": {
             "name": {"type": "string"}, "database": {"type": "string"},
             "parent": {"type": ["string", "null"]}, "at": {"type": ["string", "null"]},
             "phase": {"type": ["string", "null"]}, "node_port": {"type": ["integer", "null"]},
             "created_at": {"type": ["string", "null"]}, "ttl_seconds": {"type": ["integer", "null"]}},
             "required": ["name", "database"]}}},
        {"name": "delete_branch", "description": "Delete a branch and its compute. Refuses while the branch has child branches.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]},
         "outputSchema": {"type": "object", "description": err_note, "properties": {
             "name": {"type": "string"}, "status": {"type": "string", "enum": ["deleting", "absent"]}},
             "required": ["name", "status"]}},
        {"name": "get_connection",
         "description": "Connection URI for an existing database or branch. Wakes it if suspended (scale-to-zero) and reports the wake time.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]},
         "outputSchema": {"type": "object", "description": err_note, "properties": {
             "name": {"type": "string"}, "connection_uri": {"type": "string"},
             "woke_from_suspend": {"type": "boolean", "description": "present only when a wake happened"},
             "wake_seconds": {"type": "string", "description": "present only when a wake happened"}},
             "required": ["name", "connection_uri"]}},
        {"name": "get_metrics",
         "description": "Recent CPU/memory usage for a database or branch (15s samples, ~10 min window), with its CU limit.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]},
         "outputSchema": {"type": "object", "properties": {
             "name": {"type": "string"}, "cu_limit": {"type": "integer"},
             "priority": {"type": "string"}, "cpu_limit_millis": {"type": "integer"},
             "series": {"type": "array", "items": {"type": "object", "properties": {
                 "t": {"type": "integer"}, "cpu_millis": {"type": "integer"}, "mem_mib": {"type": "integer"}},
                 "required": ["t", "cpu_millis", "mem_mib"]}}},
             "required": ["name", "cu_limit", "series"]}},
        {"name": "get_cu_ledger",
         "description": "The compute ledger: physical CU on the cluster, CU promised as ceilings, CU awake right now, and live draw. Shows oversubscription headroom (suspended databases hold zero CU).",
         "inputSchema": {"type": "object", "properties": {}},
         "outputSchema": {"type": "object", "properties": {
             "physical_cu": {"type": "integer"}, "promised_cu": {"type": "integer"},
             "active_cu": {"type": "integer"}, "used_millis": {"type": "integer"},
             "endpoints": {"type": "integer"}, "endpoints_active": {"type": "integer"}},
             "required": ["physical_cu", "promised_cu", "active_cu"]}},
        {"name": "get_events",
         "description": "Recent lifecycle events across the estate: created, suspended, woke, TTL-reaped, enrolled.",
         "inputSchema": {"type": "object", "properties": {}},
         "outputSchema": {"type": "array", "items": {"type": "object", "properties": {
             "time": {"type": ["string", "null"]}, "reason": {"type": ["string", "null"]},
             "kind": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]},
             "message": {"type": ["string", "null"]}}}}},
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    /// T2 (RFC 012, landed by RFC 014 PR B): the tool schema is the agent
    /// contract — unintentional drift fails CI. Intentional change?
    /// UPDATE_SNAPSHOTS=1 cargo test, then review the fixture diff.
    #[test]
    fn tool_schema_snapshot() {
        let current = serde_json::to_string_pretty(&tool_defs()).unwrap();
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/mcp-tools.json");
        if std::env::var("UPDATE_SNAPSHOTS").is_ok() {
            std::fs::write(path, &current).unwrap();
            return;
        }
        let saved = std::fs::read_to_string(path)
            .expect("snapshot missing — UPDATE_SNAPSHOTS=1 cargo test to create it");
        assert_eq!(
            current, saved,
            "MCP tool schema drifted from the snapshot — if intentional, \
             UPDATE_SNAPSHOTS=1 cargo test and commit the fixture"
        );
    }

    /// The tool count is stated in README.md and docs/handbook/architecture.md
    /// — this pin makes those docs fail loudly here instead of drifting
    /// silently (review 001 P1-3).
    #[test]
    fn tool_count_is_fourteen() {
        assert_eq!(
            tool_defs().as_array().unwrap().len(),
            14,
            "tool count changed — update README.md and docs/handbook/architecture.md"
        );
    }

    /// Review 003 P1-4: every tool declares its result contract. A tool
    /// added without an outputSchema fails here; a changed one fails the
    /// snapshot above.
    #[test]
    fn every_tool_declares_output_schema() {
        for t in tool_defs().as_array().unwrap() {
            assert!(
                t.get("outputSchema").is_some(),
                "tool {} has no outputSchema",
                t["name"]
            );
        }
    }

    /// Review 003 P1-2/P1-3: boundary validation is synchronous and strict.
    #[test]
    fn invalid_inputs_are_rejected() {
        use serde_json::json;
        assert!(bounded_int(&json!({"cu_limit": -5}), "cu_limit", 1, 960).is_err());
        assert!(bounded_int(&json!({"cu_limit": 0}), "cu_limit", 1, 960).is_err());
        assert!(bounded_int(&json!({"cu_limit": 961}), "cu_limit", 1, 960).is_err());
        assert!(bounded_int(&json!({"ttl_seconds": -10}), "ttl_seconds", 1, 2592000).is_err());
        assert!(
            bounded_int(
                &json!({"suspend_after_seconds": -1}),
                "suspend_after_seconds",
                0,
                86400
            )
            .is_err()
        );
        // 0 = never suspend: explicitly allowed.
        assert_eq!(
            bounded_int(
                &json!({"suspend_after_seconds": 0}),
                "suspend_after_seconds",
                0,
                86400
            )
            .unwrap(),
            Some(0)
        );
        assert!(parse_priority(&json!({"priority": "urgent"})).is_err());
        assert_eq!(
            parse_priority(&json!({"priority": "HIGH"})).unwrap(),
            Some("High")
        );
        assert_eq!(parse_priority(&json!({})).unwrap(), None);
        // Names normalize to lowercase; invalid ones are synchronous errors.
        assert_eq!(
            valid_name(&json!({"database": "Prod"}), "database").unwrap(),
            "prod"
        );
        assert!(valid_name(&json!({"database": "no_scores"}), "database").is_err());
    }

    /// Every declared result contract must itself be a valid JSON Schema —
    /// a malformed outputSchema would pass the snapshot yet be useless to
    /// clients that compile it.
    #[test]
    fn output_schemas_compile_as_json_schema() {
        for t in tool_defs().as_array().unwrap() {
            let schema = t["outputSchema"].clone();
            if let Err(e) = jsonschema::validator_for(&schema) {
                panic!(
                    "tool {} outputSchema is not valid JSON Schema: {e}",
                    t["name"]
                );
            }
        }
    }

    /// Every tool failure is remediable by an agent (001 §5.2): the error
    /// payload always carries reason / retriable / suggested_action.
    #[test]
    fn tool_errors_are_structured() {
        let e = terr("it broke", true, "try again");
        let v: Value = serde_json::from_str(&e.to_string()).unwrap();
        assert_eq!(v["reason"], "it broke");
        assert_eq!(v["retriable"], true);
        assert_eq!(v["suggested_action"], "try again");
    }
}
