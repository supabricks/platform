//! The MCP façade (RFC 012 D8, 004 B3): streamable-HTTP JSON-RPC served from
//! the operator binary. Hand-rolled per D8's sanctioned fallback — de-risk ①
//! proved the exact surface Claude Code needs: POST JSON responses, 202 for
//! notifications, no SSE. Every tool is a thin verb over the CR model; the
//! reconcilers stay the single implementation of behavior (001 §5: one machine
//! API, many clients).

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, post};
use axum::{Json, Router};
use kube::api::{Api, DeleteParams, Patch, PatchParams};
use kube::ResourceExt;
use serde_json::{Value, json};
use tracing::info;

use crate::crd::{Branch, Database, Phase};
use crate::reconcile::{Ctx, WAKE_ANNOTATION, now_ts};

pub struct McpState {
    pub ctx: Arc<Ctx>,
    pub token: String,
    pub connect_host: String,
}

pub async fn serve(state: Arc<McpState>, addr: &str) -> anyhow::Result<()> {
    let app = Router::new()
        .route("/mcp", post(handle_post))
        .route("/mcp", any(|| async { StatusCode::METHOD_NOT_ALLOWED }))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!("MCP server listening on {addr}");
    axum::serve(listener, app).await?;
    Ok(())
}

async fn handle_post(
    State(state): State<Arc<McpState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let authed = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v == format!("Bearer {}", state.token));
    if !authed {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": "invalid authorization token"})),
        )
            .into_response();
    }
    let Ok(req) = serde_json::from_slice::<Value>(&body) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": "bad json"}))).into_response();
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
    terr(format!("kubernetes api error: {e}"), true, "retry; if it persists, check operator logs")
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
        other => Err(terr(
            format!("unknown tool {other}"),
            false,
            "call tools/list for the available tools",
        )),
    }
}

fn need_name(args: &Value) -> Result<String, ToolError> {
    args["name"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(|s| s.to_lowercase())
        .ok_or_else(|| terr("missing required argument: name", false, "pass a name argument"))
}

fn capabilities(state: &McpState) -> ToolResult {
    Ok(json!({
        "platform": "sspc", "version": env!("CARGO_PKG_VERSION"),
        "pg_version": 16,
        "max_endpoints": crate::ports::RANGE_LEN,
        "connect_host": state.connect_host,
        "features": {"branching": true, "ttl": true, "scale_to_zero": true,
                      "wake": "explicit via get_connection (plain-psql wake-on-connect: M2)"},
    }))
}

fn uri(state: &McpState, port: i32) -> String {
    format!(
        "postgresql://cloud_admin:sspc-p0@{}:{port}/postgres",
        state.connect_host
    )
}

/// Wait until the CR has a port and its compute pod is Ready. Bounded; on
/// timeout we return state honestly rather than hang (001 §5.2 "predictably
/// async" — M1's cheap version).
async fn await_ready(state: &McpState, name: &str, secs: u64) -> Option<i32> {
    let ns = &state.ctx.namespace;
    let pods: Api<k8s_openapi::api::core::v1::Pod> =
        Api::namespaced(state.ctx.client.clone(), ns);
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
    let mut spec = json!({});
    if let Some(ttl) = args["ttl_seconds"].as_i64() {
        spec["ttlSeconds"] = json!(ttl);
    }
    if let Some(s) = args["suspend_after_seconds"].as_i64() {
        spec["suspendAfterSeconds"] = json!(s);
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
            "name": name, "status": "ready", "connection_uri": uri(state, port),
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
    let database = args["database"].as_str().ok_or_else(|| {
        terr("missing required argument: database", false, "pass the parent database name")
    })?;
    let mut spec = json!({"database": database});
    if let Some(ttl) = args["ttl_seconds"].as_i64() {
        spec["ttlSeconds"] = json!(ttl);
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
            "name": name, "parent": database, "status": "ready",
            "connection_uri": uri(state, port),
        })),
        None => Ok(json!({"name": name, "status": "provisioning",
                           "note": "call get_connection to poll"})),
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
    Ok(json!(items
        .items
        .iter()
        .map(|d| {
            let mut v = summarize(d.status.as_ref());
            v["name"] = json!(d.name_any());
            v
        })
        .collect::<Vec<_>>()))
}

async fn list_branches(state: &McpState) -> ToolResult {
    let api: Api<Branch> = Api::namespaced(state.ctx.client.clone(), &state.ctx.namespace);
    let items = api.list(&Default::default()).await.map_err(from_kube)?;
    Ok(json!(items
        .items
        .iter()
        .map(|b| {
            let mut v = summarize(b.status.as_ref());
            v["name"] = json!(b.name_any());
            v["database"] = json!(b.spec.database);
            v
        })
        .collect::<Vec<_>>()))
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
            let mut out = json!({"name": name, "connection_uri": uri(state, port)});
            if woke {
                out["woke_from_suspend"] = json!(true);
                out["wake_seconds"] =
                    json!(format!("{:.1}", t0.elapsed().as_secs_f64()));
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
    match api.delete(&name, &DeleteParams::default()).await {
        Ok(_) => Ok(json!({"name": name, "status": "deleting"})),
        Err(kube::Error::Api(e)) if e.code == 404 => Ok(json!({"name": name, "status": "absent"})),
        Err(e) => Err(from_kube(e)),
    }
}

fn tool_defs() -> Value {
    let name_arg = json!({"type": "string", "description": "Resource name (lowercase DNS label)"});
    json!([
        {"name": "capabilities",
         "description": "Platform capabilities, limits, and feature flags.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "create_database",
         "description": "Create a Postgres database; returns a psql connection URI. Idempotent on name.",
         "inputSchema": {"type": "object", "properties": {
             "name": name_arg,
             "ttl_seconds": {"type": "integer", "description": "Optional TTL; the platform reaps the database when it expires"},
             "suspend_after_seconds": {"type": "integer", "description": "Idle seconds before scale-to-zero (default 300)"}},
             "required": ["name"]}},
        {"name": "list_databases", "description": "List databases with status.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "get_database", "description": "Get one database's status.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]}},
        {"name": "delete_database",
         "description": "Delete a database, its branches' parent storage, and its compute.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]}},
        {"name": "create_branch",
         "description": "Instant copy-on-write branch of a database; returns its own connection URI.",
         "inputSchema": {"type": "object", "properties": {
             "name": name_arg,
             "database": {"type": "string", "description": "Parent database name"},
             "ttl_seconds": {"type": "integer", "description": "Optional TTL (reaped in P3)"}},
             "required": ["name", "database"]}},
        {"name": "list_branches", "description": "List branches with status and parentage.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "delete_branch", "description": "Delete a branch and its compute.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]}},
        {"name": "get_connection",
         "description": "Connection URI for an existing database or branch. Wakes it if suspended (scale-to-zero) and reports the wake time.",
         "inputSchema": {"type": "object", "properties": {"name": name_arg}, "required": ["name"]}},
    ])
}
