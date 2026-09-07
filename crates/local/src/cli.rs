//! Human commands with a stable JSON result contract and deterministic exits.
use crate::{
    api::Action,
    client::{self, Client},
    daemon::Request,
    operations::BranchPoint,
    project::ProjectConfig,
    store::{Result, error::invalid},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::Read,
    path::PathBuf,
    time::{Duration, Instant},
};
use supabricks_core::resource::{DesiredState, OperationId};
const HELP: &str = r#"Supabricks local (PG17)
Usage: supabricks COMMAND [--project PATH] [--data-dir PATH] [--json]

  init NAME                    Write retry-safe public supabricks.toml (offline)
  up                           Start/reconnect using the installed native bundle
      [--bundle PATH --helpers PATH]  Override parts for source development
  down                         Stop the cell, retain all data
  status | doctor              Runtime status / actionable diagnostics
  capabilities                 Project binding, features and resource limits
  database create NAME [--key KEY] [--wait]
  database list
  branch create NAME --from PARENT [--at-lsn LSN | --at-time RFC3339]
  branch list | get NAME | use NAME | rename NAME NEW_NAME
  branch suspend NAME | resume NAME | delete NAME [--force]
  branch default NAME | ttl NAME --expires-at-ms TIMESTAMP_OR_none
  operation get ID | wait ID [--timeout-ms 90000]
  connect [BRANCH] [--uri]      Print application credentials; keep output private
  catalog [--branch NAME]       Discover application tables and columns
  sql --sql SQL | --file PATH [--branch NAME] [--write]
      [--max-rows 200] [--timeout-ms 10000]
  mcp --project PATH            MCP stdio; explicit fixed worktree required
  installation verify          Verify all installed files against the manifest

JSON is the default. Branch mutations return a durable operation immediately;
--wait polls with JSON progress on stderr, and never retries the mutation.
Use --revision N for explicit optimistic concurrency and --key KEY for retries.
SQL accepts one statement; --write requires an explicit --branch.
Default data directory: SUPABRICKS_DATA_DIR, otherwise ~/.supabricks.
Exit: 0 success/accepted, 1 local I/O, 2 input, 3 missing, 4 conflict,
      5 unavailable, 6 SQL, 7 failed/superseded operation, 8 wait deadline.
"#;
struct Args {
    pos: Vec<String>,
    flags: BTreeMap<String, String>,
}
impl Args {
    fn parse(args: Vec<String>) -> Result<Self> {
        let mut pos = Vec::new();
        let mut flags = BTreeMap::new();
        let mut iter = args.into_iter();
        while let Some(arg) = iter.next() {
            if arg.starts_with("--") {
                let value = if matches!(
                    arg.as_str(),
                    "--json" | "--wait" | "--write" | "--force" | "--uri" | "--include-deleted"
                ) {
                    "true".into()
                } else {
                    iter.next()
                        .ok_or_else(|| invalid(format!("missing value for {arg}")))?
                };
                if flags.insert(arg.clone(), value).is_some() {
                    return Err(invalid(format!("duplicate option {arg}")));
                }
            } else {
                pos.push(arg);
            }
        }
        Ok(Self { pos, flags })
    }
    fn take(&mut self, name: &str) -> Option<String> {
        self.flags.remove(name)
    }
    fn flag(&mut self, name: &str) -> bool {
        self.take(name).is_some()
    }
    fn required(&self, index: usize) -> Result<String> {
        self.pos
            .get(index)
            .cloned()
            .ok_or_else(|| invalid("missing command argument; use --help"))
    }
    fn finish(&self, count: usize) -> Result<()> {
        if self.pos.len() != count || !self.flags.is_empty() {
            return Err(invalid(format!(
                "unexpected arguments/options; use --help: {}",
                self.flags.keys().cloned().collect::<Vec<_>>().join(", ")
            )));
        }
        Ok(())
    }
    fn number<T: std::str::FromStr>(&mut self, name: &str, default: T) -> Result<T> {
        self.take(name)
            .map(|v| {
                v.parse()
                    .map_err(|_| invalid(format!("invalid number for {name}")))
            })
            .unwrap_or(Ok(default))
    }
}
pub fn run() -> Result<u8> {
    let raw: Vec<_> = std::env::args_os().skip(1).collect();
    if raw.first().is_some_and(|a| a == "child") {
        if (raw.len() != 3 && raw.len() != 4)
            || raw[1] != "--launch"
            || (raw.len() == 4 && raw[3] != "--stdin-gate")
        {
            return Err(invalid("invalid child launch"));
        }
        crate::supervisor::child(&PathBuf::from(&raw[2]), raw.len() == 4)?;
        return Ok(0);
    }
    let raw: Vec<String> = raw
        .into_iter()
        .map(|a| {
            a.into_string()
                .map_err(|_| invalid("arguments must be UTF-8"))
        })
        .collect::<Result<_>>()?;
    if raw.iter().any(|a| a == "--help" || a == "-h") {
        print!("{HELP}");
        return Ok(0);
    }
    if raw == ["--version"] {
        let version = crate::installation::Installation::discover()?
            .map(|i| i.manifest.version)
            .unwrap_or_else(|| env!("CARGO_PKG_VERSION").into());
        println!("supabricks {version} (local API 1)");
        return Ok(0);
    }
    if raw == ["installation", "verify"] {
        let installed = crate::installation::Installation::discover()?
            .ok_or_else(|| invalid("this binary is not in an installed release"))?;
        installed.verify()?;
        println!(
            "{}",
            json!({"verified":true,"version":installed.manifest.version,"identity":installed.identity,"root":installed.root})
        );
        return Ok(0);
    }
    let mut a = Args::parse(raw)?;
    let command = a.required(0)?;
    let root = if let Some(root) = a
        .take("--data-dir")
        .or_else(|| std::env::var("SUPABRICKS_DATA_DIR").ok())
    {
        PathBuf::from(root)
    } else {
        PathBuf::from(std::env::var_os("HOME").ok_or_else(|| invalid("set --data-dir or HOME"))?)
            .join(".supabricks")
    };
    let root = if root.is_absolute() {
        root
    } else {
        std::env::current_dir()?.join(root)
    };
    let project = a.take("--project").map(PathBuf::from);
    a.flag("--json");
    if matches!(command.as_str(), "daemon" | "up" | "down" | "status") {
        let bundle = a.take("--bundle").map(PathBuf::from);
        let helpers = a.take("--helpers").map(PathBuf::from);
        if bundle.is_some() != helpers.is_some()
            || (bundle.is_some() && !matches!(command.as_str(), "up" | "daemon"))
        {
            return Err(invalid(
                "--bundle and --helpers must be used together on up",
            ));
        }
        a.finish(1)?;
        if let Some(project) = &project {
            ProjectConfig::read(project)?;
        }
        crate::runtime_cli::run(&command, root, bundle, helpers)?;
        return Ok(0);
    }
    if command == "doctor" {
        a.finish(1)?;
        let status = client::request(&root, Request::Status);
        let project_check = project.as_ref().map(|p| {
            ProjectConfig::read(p)
                .map(|c| json!({"id":c.id,"name":c.name}))
                .unwrap_or_else(|e| json!({"error":client::diagnostic(&e)}))
        });
        let healthy = status
            .as_ref()
            .is_ok_and(|s| s["runtime"]["ready"] == true && s["runtime"]["last_error"].is_null())
            && project_check
                .as_ref()
                .is_none_or(|p| p.get("error").is_none());
        println!(
            "{}",
            json!({"healthy":healthy,"data_dir":root,"project":project_check,"runtime":status.unwrap_or_else(|e|json!({"error":client::diagnostic(&e)})),"hint":"run supabricks up; source builds need --bundle and --helpers; run operation get ID for branch progress; daemon.log is private"})
        );
        return Ok(if healthy { 0 } else { 5 });
    }
    if command == "init" {
        let name = a.required(1)?;
        a.finish(2)?;
        let directory = project.unwrap_or(std::env::current_dir()?);
        let config = ProjectConfig::initialize(&directory, &name)?;
        println!(
            "{}",
            json!({"project":config,"worktree":directory.canonicalize()?})
        );
        return Ok(0);
    }
    if command == "mcp" && project.is_none() {
        return Err(invalid("mcp requires an explicit --project worktree path"));
    }
    let directory = client::project_directory(project.as_deref())?;
    let c = Client::bind(&root, &directory)?;
    if command == "mcp" {
        a.finish(1)?;
        crate::mcp::serve(c)?;
        return Ok(0);
    }
    let wait = a.flag("--wait");
    if wait
        && !(matches!(command.as_str(), "database" | "branch")
            && a.pos.get(1).is_some_and(|v| {
                matches!(
                    v.as_str(),
                    "create" | "resume" | "suspend" | "delete" | "default" | "ttl"
                )
            }))
    {
        return Err(invalid("--wait requires a lifecycle operation"));
    }
    let action = match command.as_str() {
        "capabilities" => {
            a.finish(1)?;
            Action::Capabilities
        }
        "connect" => {
            let uri = a.flag("--uri");
            let branch = a.pos.get(1).cloned();
            a.finish(if branch.is_some() { 2 } else { 1 })?;
            let result = c.call(Action::Connect { branch })?;
            if uri {
                println!(
                    "{}",
                    result["uri"]
                        .as_str()
                        .ok_or_else(|| invalid("connection URI unavailable"))?
                );
            } else {
                println!("{result}");
            }
            return Ok(0);
        }
        "catalog" => {
            let branch = a.take("--branch");
            a.finish(1)?;
            Action::Catalog { branch }
        }
        "sql" => {
            let branch = a.take("--branch");
            let read_only = !a.flag("--write");
            let sql = a.take("--sql");
            let file = a.take("--file");
            if sql.is_some() == file.is_some() {
                return Err(invalid("supply exactly one of --sql or --file"));
            }
            let sql = if let Some(sql) = sql {
                sql
            } else {
                let mut sql = String::new();
                std::fs::File::open(file.unwrap())?
                    .take(32769)
                    .read_to_string(&mut sql)?;
                sql
            };
            let max_rows = a.number("--max-rows", 200)?;
            let timeout_ms = a.number("--timeout-ms", 10000)?;
            a.finish(1)?;
            if !read_only && branch.is_none() {
                return Err(invalid("SQL writes require --branch NAME"));
            }
            Action::Sql {
                branch,
                sql,
                read_only,
                max_rows,
                timeout_ms,
            }
        }
        "operation" => {
            let verb = a.required(1)?;
            let id = a
                .required(2)?
                .parse()
                .map_err(|_| invalid("invalid operation ID"))?;
            let timeout = a.number("--timeout-ms", 90000)?;
            a.finish(3)?;
            if verb == "wait" {
                return wait_operation(&c, id, timeout);
            }
            if verb != "get" {
                return Err(invalid("operation requires get or wait"));
            }
            Action::GetOperation { id }
        }
        "database" | "branch" => {
            let verb = a.required(1)?;
            if verb == "list" {
                let include_deleted = a.flag("--include-deleted");
                a.finish(2)?;
                Action::ListBranches { include_deleted }
            } else {
                let mut branch = a.required(2)?;
                if command == "database" && verb != "create" {
                    return Err(invalid(
                        "database supports create and list; manage roots with branch commands",
                    ));
                }
                let key = a
                    .take("--key")
                    .unwrap_or_else(|| OperationId::new().to_string());
                let action = match verb.as_str() {
                    "create" if command == "database" => {
                        Action::CreateDatabase { name: branch, key }
                    }
                    "create" => {
                        let parent = a
                            .take("--from")
                            .ok_or_else(|| invalid("branch create requires --from PARENT"))?;
                        let lsn = a.take("--at-lsn");
                        let time = a.take("--at-time");
                        if lsn.is_some() && time.is_some() {
                            return Err(invalid("choose --at-lsn or --at-time"));
                        }
                        let point = if let Some(lsn) = lsn {
                            BranchPoint::Lsn {
                                lsn: lsn.parse().map_err(|_| invalid("invalid LSN"))?,
                            }
                        } else if let Some(timestamp) = time {
                            BranchPoint::Time { timestamp }
                        } else {
                            BranchPoint::Head
                        };
                        Action::CreateBranch {
                            name: branch,
                            parent,
                            key,
                            point,
                        }
                    }
                    "get" => Action::GetBranch { branch },
                    "use" => Action::SelectBranch { branch },
                    "rename" => Action::RenameBranch {
                        branch,
                        name: a.required(3)?,
                    },
                    "default" => Action::SetDefault { branch, key },
                    "resume" | "suspend" | "delete" | "ttl" => {
                        let explicit = a.take("--revision");
                        let record = c.call(Action::GetBranch {
                            branch: branch.clone(),
                        })?;
                        branch = record["branch"]["id"]
                            .as_str()
                            .ok_or_else(|| invalid("missing branch ID"))?
                            .to_owned();
                        let expected_revision = if let Some(r) = explicit {
                            r.parse().map_err(|_| invalid("invalid revision"))?
                        } else {
                            // Resolve first, then send the ID and revision together. A
                            // concurrent rename/recreate cannot retarget this mutation.
                            record["revision"]
                                .as_i64()
                                .ok_or_else(|| invalid("missing branch revision"))?
                        };
                        match verb.as_str() {
                            "delete" => Action::DeleteBranch {
                                branch,
                                expected_revision,
                                key,
                                force: a.flag("--force"),
                            },
                            "ttl" => {
                                let t = a.take("--expires-at-ms").ok_or_else(|| {
                                    invalid("ttl requires --expires-at-ms TIMESTAMP or none")
                                })?;
                                let expires_at_ms = if t == "none" {
                                    None
                                } else {
                                    Some(t.parse().map_err(|_| invalid("invalid expiration"))?)
                                };
                                Action::SetTtl {
                                    branch,
                                    expected_revision,
                                    expires_at_ms,
                                    key,
                                }
                            }
                            _ => Action::SetState {
                                branch,
                                expected_revision,
                                desired: if verb == "resume" {
                                    DesiredState::Running
                                } else {
                                    DesiredState::Suspended
                                },
                                key,
                            },
                        }
                    }
                    _ => return Err(invalid("unknown branch command")),
                };
                a.finish(if verb == "rename" { 4 } else { 3 })?;
                action
            }
        }
        _ => return Err(invalid("unknown command; use --help")),
    };
    let result = c.call(action)?;
    if wait {
        eprintln!("{}", json!({"accepted":result}));
        let id = serde_json::from_value(result["id"].clone())
            .map_err(|_| invalid("--wait requires a lifecycle operation"))?;
        return wait_operation(&c, id, 90000);
    }
    println!("{result}");
    Ok(operation_exit(&result))
}
fn operation_exit(v: &Value) -> u8 {
    if v["status"] == "failed" || v["status"] == "superseded" {
        7
    } else {
        0
    }
}
fn wait_operation(c: &Client, id: OperationId, timeout: u64) -> Result<u8> {
    if !(100..=300000).contains(&timeout) {
        return Err(invalid("wait timeout must be 100–300000 ms"));
    }
    let deadline = Instant::now() + Duration::from_millis(timeout);
    let mut previous = Value::Null;
    loop {
        let op = c.call(Action::GetOperation { id })?;
        if op["status"] != "pending" {
            println!("{op}");
            return Ok(operation_exit(&op));
        }
        if op != previous {
            eprintln!("{}", json!({"progress":op}));
            previous = op.clone();
        }
        if Instant::now() >= deadline {
            println!(
                "{}",
                json!({"operation":op,"waiting":true,"hint":"operation continues; resume with operation wait ID"})
            );
            return Ok(8);
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}
