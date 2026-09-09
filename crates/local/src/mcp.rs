//! Stdio MCP adapter. It cannot change its project/worktree binding or stop the cell.
use crate::{
    api::Action,
    client::{Client, diagnostic},
    store::Result,
};
use serde_json::{Value, json};
use std::io::{BufRead, Read, Write};

pub fn tools() -> Value {
    let string = json!({"type":"string"});
    let branch = json!({"type":"string","description":"Explicit project-local branch name or UUID. Omit only on read/SQL tools to use this worktree's selection."});
    let key = json!({"type":"string","minLength":1,"maxLength":256,"description":"Unique idempotency key. Reuse only for an identical retry; poll the returned operation ID."});
    let revision = json!({"type":"integer","minimum":1,"description":"Revision returned by get_branch. Stale mutations fail with conflict."});
    let data_type = json!({"oneOf":[{"type":"object","additionalProperties":false,"properties":{"kind":{"enum":["text","boolean","smallint","integer","bigint","double","date","timestamp","timestamp_tz"]}},"required":["kind"]},{"type":"object","additionalProperties":false,"properties":{"kind":{"const":"decimal"},"precision":{"type":"integer","minimum":1,"maximum":38},"scale":{"type":"integer","minimum":0,"maximum":38}},"required":["kind","precision","scale"]}]});
    let mapping = json!({"type":"object","additionalProperties":false,"description":"Explicitly approved inspection mapping. Inputs are zero-based source index strings, each used once. Text preserves leading zeros; typed conversions reject overflow or rounding.","properties":{"version":{"const":1},"format":{"const":"csv"},"delimiter":{"enum":[",","\t",";","|"]},"header":{"type":"boolean"},"null_strings":{"type":"array","maxItems":16,"items":{"type":"string","maxLength":256}},"columns":{"type":"array","minItems":1,"maxItems":256,"items":{"type":"object","additionalProperties":false,"properties":{"input":{"type":"string","pattern":"^[0-9]+$"},"name":{"type":"string","minLength":1,"maxLength":63},"data_type":data_type,"nullable":{"type":"boolean"}},"required":["input","name","data_type","nullable"]}}},"required":["version","format","delimiter","header","null_strings","columns"]});
    let load = json!({"type":"object","additionalProperties":false,"properties":{"version":{"const":1},"project_id":string,"branch_id":string,"branch_revision":revision,"source_id":string,"source_sha256":string,"schema":string,"table":string,"mapping":mapping},"required":["version","project_id","branch_id","branch_revision","source_id","source_sha256","schema","table","mapping"]});
    let defs = vec![
        (
            "ingest_inspect",
            "Copy and inspect an explicitly requested absolute local CSV/TSV file. Returns a source ID immediately; poll ingest_source. Preview is a sample, text mappings preserve leading zeros. No database write.",
            json!({"path":string,"delimiter":{"enum":[",","\t",";","|"]},"header":{"type":"boolean","default":true},"null_strings":{"type":"array","items":string,"maxItems":16}}),
            vec!["path"],
            false,
        ),
        (
            "ingest_source",
            "Read source status and bounded inspection. Preview samples are ephemeral; staged bytes remain private and immutable.",
            json!({"id":string}),
            vec!["id"],
            true,
        ),
        (
            "ingest_load",
            "Load a staged source into a NEW PostgreSQL table using an explicitly approved mapping and branch revision. Obtain write authorization naming the branch/table. Returns durable job; poll ingest_status. Never infer schema silently.",
            json!({"load":load,"key":{"type":"string","minLength":1,"maxLength":128}}),
            vec!["load", "key"],
            false,
        ),
        (
            "ingest_status",
            "Read the durable import outcome, source and progress; reconciling is not a safe retry.",
            json!({"id":string}),
            vec!["id"],
            true,
        ),
        (
            "ingest_list",
            "List recent project-bound imports, optionally filtered by branch.",
            json!({"branch":branch,"limit":{"type":"integer","minimum":1,"maximum":100,"default":20}}),
            vec![],
            true,
        ),
        (
            "ingest_find",
            "Find a retained import by explicit branch and idempotency key before deciding whether to retry.",
            json!({"branch":branch,"key":{"type":"string","minLength":1,"maxLength":128}}),
            vec!["branch", "key"],
            true,
        ),
        (
            "ingest_cancel",
            "Cancel an import. A commit in flight remains reconciling until its receipt is resolved; poll ingest_status.",
            json!({"id":string}),
            vec!["id"],
            false,
        ),
        (
            "ingest_retry",
            "Explicitly retry a failed import only after proven rollback, with its unchanged approved mapping/source and branch revision.",
            json!({"id":string}),
            vec!["id"],
            false,
        ),
        (
            "ingest_dispose",
            "Dispose an inactive staged source and revoke retained retry references. Active imports prevent disposal; the original device file is untouched.",
            json!({"id":string}),
            vec!["id"],
            false,
        ),
        (
            "analytics_refresh",
            "Export a frozen Postgres branch and atomically publish a new analytical epoch. Returns an ID; poll analytics_status. Existing sessions remain pinned.",
            json!({"branch":branch,"key":key}),
            vec!["branch", "key"],
            false,
        ),
        (
            "analytics_status",
            "Inspect refresh progress from frozen export through complete epoch publication.",
            json!({"id":string}),
            vec!["id"],
            true,
        ),
        (
            "analytics_cancel_refresh",
            "Cancel a refresh and reclaim unpublished data. Published snapshots use retention instead.",
            json!({"id":string}),
            vec!["id"],
            false,
        ),
        (
            "analytics_open",
            "Open a bounded Sail session on a selected epoch or current branch snapshot. First access automatically refreshes if no snapshot exists. Poll analytics_session until ready for the Spark Connect endpoint.",
            json!({"branch":branch,"epoch":string,"key":key,"ttl_ms":{"type":"integer","minimum":10000,"maximum":3600000,"default":900000}}),
            vec!["key"],
            false,
        ),
        (
            "analytics_session",
            "Inspect session lifecycle, Spark Connect endpoint and pinned epoch metadata. Raw PySpark can query _supabricks.epoch.",
            json!({"id":string}),
            vec!["id"],
            true,
        ),
        (
            "analytics_close",
            "Close an analytical session, stop its worker, and release its epoch reference after confirmed process cleanup.",
            json!({"id":string}),
            vec!["id"],
            false,
        ),
        (
            "analytics_sql",
            "Submit one SELECT/WITH/EXPLAIN read query to a ready analytical session. Returns a query ID; poll analytics_query. Results are bounded text values or null, preserving exact decimals. Only the latest result is retained.",
            json!({"id":string,"sql":{"type":"string","minLength":1,"maxLength":32768},"max_rows":{"type":"integer","minimum":1,"maximum":1000,"default":200},"max_bytes":{"type":"integer","minimum":1024,"maximum":262144,"default":262144},"timeout_ms":{"type":"integer","minimum":100,"maximum":30000,"default":10000}}),
            vec!["id", "sql"],
            false,
        ),
        (
            "analytics_query",
            "Read the latest query status and bounded result for a session. Starting another request replaces the previous result.",
            json!({"id":string,"query":string}),
            vec!["id", "query"],
            true,
        ),
        (
            "analytics_cancel",
            "Cancel all execution in a session by closing its worker. This also disconnects raw Spark Connect clients and releases the epoch reference after confirmed cleanup.",
            json!({"id":string}),
            vec!["id"],
            false,
        ),
        (
            "capabilities",
            "Report this session's project/worktree, local API version, features and hard limits.",
            json!({}),
            vec![],
            true,
        ),
        (
            "list_branches",
            "List databases and branches in this project with identity, ancestry, revisions and desired/observed state.",
            json!({"include_deleted":{"type":"boolean"}}),
            vec![],
            true,
        ),
        (
            "get_branch",
            "Inspect one branch, including lifecycle revision and default/TTL protections.",
            json!({"branch":branch}),
            vec!["branch"],
            true,
        ),
        (
            "selection",
            "Inspect this worktree's explicitly selected branch.",
            json!({}),
            vec![],
            true,
        ),
        (
            "select_branch",
            "Persist this worktree's branch selection; other worktrees are unchanged.",
            json!({"branch":branch}),
            vec!["branch"],
            false,
        ),
        (
            "create_database",
            "Create an independent PG17 database root. Returns an operation immediately; poll get_operation until succeeded.",
            json!({"name":string,"key":key}),
            vec!["name", "key"],
            false,
        ),
        (
            "create_branch",
            "Fork an explicit parent at head, exact LSN or retained timestamp. Returns a durable operation; poll get_operation.",
            json!({"name":string,"parent":branch,"key":key,"point":{"oneOf":[{"type":"object","properties":{"kind":{"const":"head"}},"required":["kind"],"additionalProperties":false},{"type":"object","properties":{"kind":{"const":"lsn"},"lsn":string},"required":["kind","lsn"],"additionalProperties":false},{"type":"object","properties":{"kind":{"const":"time"},"timestamp":string},"required":["kind","timestamp"],"additionalProperties":false}]}}),
            vec!["name", "parent", "key"],
            false,
        ),
        (
            "set_state",
            "Request running or suspended for a named branch at its current revision. Active connections/pins block suspension. Returns an operation.",
            json!({"branch":branch,"expected_revision":revision,"desired":{"enum":["running","suspended"]},"key":key}),
            vec!["branch", "expected_revision", "desired", "key"],
            false,
        ),
        (
            "delete_branch",
            "Permanently remove a named branch and its data. Children always block deletion; force also disconnects clients and removes default protection. Returns an operation.",
            json!({"branch":branch,"expected_revision":revision,"key":key,"force":{"type":"boolean","default":false}}),
            vec!["branch", "expected_revision", "key"],
            false,
        ),
        (
            "rename_branch",
            "Rename a named branch; its stable connection and lifecycle revision remain valid.",
            json!({"branch":branch,"name":string}),
            vec!["branch", "name"],
            false,
        ),
        (
            "set_default",
            "Change this project's protected default branch. Does not change any worktree selection.",
            json!({"branch":branch,"key":key}),
            vec!["branch", "key"],
            false,
        ),
        (
            "set_ttl",
            "Set a future expiration in Unix milliseconds or null to clear. Default branches reject TTL; expiry drains clients then deletes data.",
            json!({"branch":branch,"expected_revision":revision,"expires_at_ms":{"type":["integer","null"]},"key":key}),
            vec!["branch", "expected_revision", "expires_at_ms", "key"],
            false,
        ),
        (
            "get_operation",
            "Observe a durable operation's status, step checkpoints and diagnostic error. Pending acceptance is not completion.",
            json!({"id":string}),
            vec!["id"],
            true,
        ),
        (
            "connect",
            "Return a stable PostgreSQL URI and application credentials. Treat output as secret; SQL connection wakes compute.",
            json!({"branch":branch}),
            vec![],
            true,
        ),
        (
            "catalog",
            "Discover tables and columns using the application role and SQL limits. Wakes the selected or explicit branch.",
            json!({"branch":branch}),
            vec![],
            true,
        ),
        (
            "sql",
            "Execute one bounded PostgreSQL statement. Read-only by default; migrations require read_only=false and an explicit branch. Returns text values and column types. Never automatically retry writes.",
            json!({"branch":branch,"sql":{"type":"string","minLength":1,"maxLength":32768},"read_only":{"type":"boolean","default":true},"max_rows":{"type":"integer","minimum":1,"maximum":1000,"default":200},"timeout_ms":{"type":"integer","minimum":100,"maximum":30000,"default":10000}}),
            vec!["sql"],
            false,
        ),
    ];
    Value::Array(defs.into_iter().map(|(name,description,properties,required,read)|json!({"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":false},"outputSchema":output_schema(name),"annotations":{"readOnlyHint":read,"destructiveHint":!read,"openWorldHint":false}})).collect())
}
fn output_schema(name: &str) -> Value {
    let string = json!({"type":"string"});
    let operation = json!({"type":"object","properties":{"id":string,"project_id":string,"branch_id":string,"revision":{"type":"integer"},"status":{"enum":["pending","succeeded","failed","superseded"]},"steps":{"type":"array","items":{"type":"string"}},"next_step":{"type":"integer"},"results":{"type":"array"},"error":{"type":["object","null"]}},"required":["id","project_id","branch_id","revision","status","steps","next_step","results","error"]});
    let branch = json!({"type":"object","properties":{"branch":{"type":"object","required":["id","project_id","name","parent_id"]},"endpoint":{"type":"object","required":["id","desired_state"]},"revision":{"type":"integer"},"observed_revision":{"type":"integer"},"is_default":{"type":"boolean"},"expired":{"type":"boolean"}},"required":["branch","endpoint","revision","observed_revision","is_default","expired"]});
    let success = match name {
        "ingest_inspect" | "ingest_source" => {
            json!({"type":"object","required":["source","inspection","error","progress"]})
        }
        "ingest_load" | "ingest_status" | "ingest_cancel" | "ingest_retry" => {
            json!({"type":"object","required":["id","load","state","parsed_rows","copied_rows","committed_rows","source"]})
        }
        "ingest_list" => {
            json!({"type":"object","properties":{"jobs":{"type":"array","items":{"type":"object","required":["id","load","state"]}}},"required":["jobs"]})
        }
        "ingest_find" => {
            json!({"type":"object","properties":{"job":{"type":["object","null"]}},"required":["job"]})
        }
        "ingest_dispose" => json!({"type":"object","required":["disposed","source_id"]}),
        "analytics_refresh"
        | "analytics_status"
        | "analytics_cancel_refresh"
        | "analytics_open"
        | "analytics_session"
        | "analytics_close"
        | "analytics_sql"
        | "analytics_query"
        | "analytics_cancel" => json!({"type":"object","required":["id","state"]}),
        "create_database" | "create_branch" | "set_state" | "delete_branch" | "set_default"
        | "set_ttl" | "get_operation" => operation,
        "get_branch" | "rename_branch" => branch,
        "list_branches" => {
            json!({"type":"object","properties":{"branches":{"type":"array","items":branch}},"required":["branches"]})
        }
        "select_branch" | "selection" => {
            json!({"type":"object","properties":{"branch_id":string},"required":["branch_id"]})
        }
        "connect" => {
            json!({"type":"object","properties":{"branch_id":string,"uri":string,"host":string,"port":{"type":"integer"},"username":string,"password":string,"database":string},"required":["branch_id","uri","host","port","username","password","database"]})
        }
        "sql" | "catalog" => {
            json!({"type":"object","properties":{"branch_id":string,"columns":{"type":"array","items":{"type":"object","required":["name","type","oid"]}},"rows":{"type":"array","items":{"type":"array","items":{"type":["string","null"]}}},"affected_rows":{"type":"integer"},"read_only":{"type":"boolean"}},"required":["branch_id","columns","rows","affected_rows","read_only"]})
        }
        _ => {
            json!({"type":"object","required":["api","api_version","project_id","worktree","features","limits"]})
        }
    };
    json!({"type":"object","oneOf":[success,{"type":"object","properties":{"error":{"type":"object","required":["code","message","hint","retryable","exit_code"]}},"required":["error"]}]})
}
fn rpc_error(id: Value, code: i32, message: &str) -> Value {
    json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":message}})
}
#[derive(Default)]
pub struct Session {
    initialized: bool,
    ready: bool,
}
impl Session {
    pub fn dispatch(&mut self, client: &Client, value: Value) -> Option<Value> {
        let id = value.get("id").cloned();
        if value["jsonrpc"] != "2.0"
            || !value["method"].is_string()
            || id.as_ref().is_some_and(|v| !v.is_string() && !v.is_i64())
        {
            return Some(rpc_error(
                id.unwrap_or(Value::Null),
                -32600,
                "invalid JSON-RPC request",
            ));
        }
        let method = value["method"].as_str().unwrap();
        if id.is_none() {
            if method == "notifications/initialized" && self.initialized {
                self.ready = true;
            }
            return None;
        }
        let id = id.unwrap();
        let params = value.get("params").cloned().unwrap_or(json!({}));
        let result = match method {
            "initialize" if !self.initialized => {
                if !params["protocolVersion"].is_string()
                    || !params["clientInfo"].is_object()
                    || !params["capabilities"].is_object()
                {
                    return Some(rpc_error(
                        id,
                        -32602,
                        "initialize requires protocolVersion, clientInfo and capabilities",
                    ));
                }
                self.initialized = true;
                json!({"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"supabricks-local","version":env!("CARGO_PKG_VERSION")},"instructions":"Project/worktree binding is fixed. Discover capabilities, inspect branch revisions, poll durable operations. Branch before migrations; verify parent isolation. Connection outputs contain secrets."})
            }
            "ping" => json!({}),
            _ if !self.ready => {
                return Some(rpc_error(
                    id,
                    -32600,
                    "initialize and notifications/initialized are required",
                ));
            }
            "tools/list" => {
                if params.get("cursor").is_some() {
                    return Some(rpc_error(
                        id,
                        -32602,
                        "this tool list has no pagination cursor",
                    ));
                }
                json!({"tools":tools()})
            }
            "tools/call" => {
                let name = params["name"].as_str().unwrap_or("");
                if !tools()
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|t| t["name"] == name)
                {
                    return Some(rpc_error(id, -32602, "unknown local tool"));
                }
                let mut args = params.get("arguments").cloned().unwrap_or(json!({}));
                if !args.is_object() || args.get("action").is_some() {
                    return Some(rpc_error(
                        id,
                        -32602,
                        "tool arguments must be an object without action",
                    ));
                }
                args["action"] = json!(name);
                let action = match serde_json::from_value::<Action>(args) {
                    Ok(a) => a,
                    Err(_) => {
                        return Some(rpc_error(
                            id,
                            -32602,
                            "invalid tool arguments; consult inputSchema",
                        ));
                    }
                };
                let (body, error) = match client.call(action) {
                    Ok(v) => (
                        match name {
                            "ingest_list" => json!({"jobs":v}),
                            "ingest_find" => json!({"job":v}),
                            _ => v,
                        },
                        false,
                    ),
                    Err(e) => (json!({"error":diagnostic(&e)}), true),
                };
                json!({"content":[{"type":"text","text":body.to_string()}],"structuredContent":body,"isError":error})
            }
            _ => return Some(rpc_error(id, -32601, "method not found")),
        };
        Some(json!({"jsonrpc":"2.0","id":id,"result":result}))
    }
}
pub fn serve(client: Client) -> Result<()> {
    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    let mut session = Session::default();
    loop {
        let mut bytes = Vec::new();
        let n = (&mut input).take(65537).read_until(b'\n', &mut bytes)?;
        if n == 0 {
            return Ok(());
        }
        if bytes.len() > 65536 || bytes.last() != Some(&b'\n') {
            writeln!(
                output,
                "{}",
                rpc_error(
                    Value::Null,
                    -32600,
                    "request exceeds 64 KiB or lacks newline"
                )
            )?;
            output.flush()?;
            return Ok(());
        }
        let reply = match serde_json::from_slice(&bytes) {
            Ok(value) => session.dispatch(&client, value),
            Err(_) => Some(rpc_error(Value::Null, -32700, "invalid JSON")),
        };
        if let Some(reply) = reply {
            writeln!(output, "{reply}")?;
            output.flush()?;
        }
    }
}
