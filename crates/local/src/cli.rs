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
  analytics configure --python PATH --worker PATH  Configure the A01 developer worker
  analytics export --branch NAME [--key KEY] [--max-bytes N] [--timeout-ms N]
  analytics status ID | cancel ID
  analytics refresh --branch NAME [--key KEY] [--wait]
  analytics open [--branch NAME] [--epoch ID] [--ttl-ms 900000] [--wait]
  analytics session ID | close ID | cancel-session ID
  analytics sql --sql SQL [--session ID | --branch NAME] [--max-rows 200]
  analytics query SESSION_ID QUERY_ID
  spark shell [--branch NAME] [--epoch ID] [--file SCRIPT.py]
  analytics publish EXPORT_ID | publication EXPORT_ID | discard EXPORT_ID
  analytics snapshot --branch NAME | epochs --branch NAME | epoch EPOCH_ID
  analytics pin EPOCH_ID | renew LEASE_ID [--ttl-ms 60000] | unpin LEASE_ID
  analytics gc --branch NAME [--keep 2]
  operation get ID | wait ID [--timeout-ms 90000]
  connect [BRANCH] [--uri]      Print application credentials; keep output private
  catalog [--branch NAME]       Discover application tables and columns
  sql --sql SQL | --file PATH [--branch NAME] [--write]
      [--max-rows 200] [--timeout-ms 10000]
  mcp --project PATH            MCP stdio; explicit fixed worktree required
  backup create PATH           Stop and capture a verified private recovery bundle
  backup verify PATH           Verify every recovery file without starting runtime
  backup restore PATH [--release PATH]  Restore into a new --data-dir with source release
  installation verify          Verify all installed files against the manifest
  installation upgrade --prefix PATH --previous PATH --backup PATH
                               Run from the staged candidate; retain source backup
  installation uninstall       Stop this cell; remove command links, retain data/versions

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
    if command == "backup" {
        let action = a.required(1)?;
        let path = PathBuf::from(a.required(2)?);
        let source_release = if action == "restore" {
            a.take("--release").map(PathBuf::from)
        } else {
            None
        };
        a.finish(3)?;
        let result = match action.as_str() {
            "create" => crate::recovery::create(&root, &path)?,
            "verify" => {
                let m = crate::recovery::verify(&path)?;
                json!({"verified":true,"id":m.id,"files":m.files.len(),"contains_credentials":true})
            }
            "restore" => {
                crate::recovery::restore_with_release(&path, &root, source_release.as_deref())?
            }
            _ => return Err(invalid("use backup create, verify or restore")),
        };
        println!("{result}");
        return Ok(0);
    }
    if command == "installation" {
        let action = a.required(1)?;
        let result = if action == "upgrade" {
            let mut required = |name| {
                a.take(name)
                    .map(PathBuf::from)
                    .ok_or_else(|| invalid(format!("missing {name}")))
            };
            let prefix = required("--prefix")?;
            let previous = required("--previous")?;
            let backup = required("--backup")?;
            a.finish(2)?;
            crate::upgrade::run(&root, &prefix, &previous, &backup)?
        } else if action == "uninstall" {
            a.finish(2)?;
            crate::upgrade::uninstall(&root)?
        } else {
            return Err(invalid("use installation verify, upgrade or uninstall"));
        };
        println!("{result}");
        return Ok(0);
    }
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
    if command == "spark" {
        if a.required(1)? != "shell" {
            return Err(invalid("use spark shell"));
        }
        let branch = a.take("--branch");
        let epoch = a
            .take("--epoch")
            .map(|s| s.parse())
            .transpose()
            .map_err(|_| invalid("invalid epoch ID"))?;
        let ttl_ms = a.number("--ttl-ms", 900000)?;
        let file = a.take("--file");
        a.finish(2)?;
        let session = c.call(Action::AnalyticsOpen {
            branch,
            epoch,
            key: OperationId::new().to_string(),
            ttl_ms,
        })?;
        let id = serde_json::from_value(session["id"].clone())?;
        let result = (|| {
            let session = wait_analytics(&c, Action::AnalyticsSession { id }, 120_000 + ttl_ms)?;
            if session["state"] != "ready" {
                println!("{session}");
                return Ok(7);
            }
            let (python, exporter) = crate::installation::analytical_worker(&root)?;
            let worker = exporter.with_file_name("shell.py");
            let mut cmd = std::process::Command::new(python);
            cmd.arg(worker)
                .arg("--endpoint")
                .arg(session["endpoint"].as_str().unwrap())
                .arg("--root")
                .arg(&root)
                .arg("--binding")
                .arg(serde_json::to_string(&c.binding)?)
                .arg("--session")
                .arg(id.to_string());
            if let Some(file) = file {
                cmd.arg("--file").arg(file);
            }
            Ok(cmd.status()?.code().unwrap_or(1).clamp(0, 255) as u8)
        })();
        let cleanup = close_analytics(&c, id);
        return match result {
            Ok(code) => {
                cleanup?;
                Ok(code)
            }
            Err(e) => Err(e),
        };
    }
    if command == "analytics" && a.required(1)? == "sql" {
        let sql = sql_argument(&mut a)?;
        let session = a
            .take("--session")
            .map(|s| s.parse())
            .transpose()
            .map_err(|_| invalid("invalid session ID"))?;
        let branch = a.take("--branch");
        if session.is_some() && branch.is_some() {
            return Err(invalid("choose --session or --branch"));
        }
        let max_rows = a.number("--max-rows", 200)?;
        let max_bytes = a.number("--max-bytes", 262144)?;
        let timeout_ms = a.number("--timeout-ms", 10000)?;
        a.finish(2)?;
        let owned = session.is_none();
        let id = match session {
            Some(id) => id,
            None => serde_json::from_value(
                c.call(Action::AnalyticsOpen {
                    branch,
                    epoch: None,
                    key: OperationId::new().to_string(),
                    ttl_ms: 600000,
                })?["id"]
                    .clone(),
            )?,
        };
        let result = (|| {
            let session = wait_analytics(&c, Action::AnalyticsSession { id }, 600000)?;
            if session["state"] != "ready" {
                println!("{session}");
                return Ok(7);
            }
            let accepted = c.call(Action::AnalyticsSql {
                id,
                sql,
                max_rows,
                max_bytes,
                timeout_ms,
            })?;
            let query = serde_json::from_value(accepted["id"].clone())?;
            let result =
                wait_analytics(&c, Action::AnalyticsQuery { id, query }, timeout_ms + 15000)?;
            println!("{result}");
            Ok(if result["state"] == "complete" { 0 } else { 7 })
        })();
        if owned {
            let cleanup = close_analytics(&c, id);
            if result.is_ok() {
                cleanup?;
            }
        }
        return result;
    }
    let wait = a.flag("--wait");
    if wait
        && !(command == "analytics"
            && a.pos.get(1).is_some_and(|v| {
                matches!(v.as_str(), "refresh" | "open" | "close" | "cancel-session")
            }))
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
        "analytics" => match a.required(1)?.as_str() {
            "open" => {
                let branch = a.take("--branch");
                let epoch = a
                    .take("--epoch")
                    .map(|s| s.parse())
                    .transpose()
                    .map_err(|_| invalid("invalid epoch ID"))?;
                let key = a
                    .take("--key")
                    .unwrap_or_else(|| OperationId::new().to_string());
                let ttl_ms = a.number("--ttl-ms", 900000)?;
                a.finish(2)?;
                Action::AnalyticsOpen {
                    branch,
                    epoch,
                    key,
                    ttl_ms,
                }
            }
            verb @ ("session" | "close" | "cancel-session" | "cancel-refresh") => {
                let id = a
                    .required(2)?
                    .parse()
                    .map_err(|_| invalid("invalid analytical ID"))?;
                a.finish(3)?;
                match verb {
                    "session" => Action::AnalyticsSession { id },
                    "close" => Action::AnalyticsClose { id },
                    "cancel-refresh" => Action::AnalyticsCancelRefresh { id },
                    _ => Action::AnalyticsCancel { id },
                }
            }
            "query" => {
                let id = a
                    .required(2)?
                    .parse()
                    .map_err(|_| invalid("invalid session ID"))?;
                let query = a
                    .required(3)?
                    .parse()
                    .map_err(|_| invalid("invalid query ID"))?;
                a.finish(4)?;
                Action::AnalyticsQuery { id, query }
            }
            verb @ ("publish" | "publication" | "discard") => {
                let id = a
                    .required(2)?
                    .parse()
                    .map_err(|_| invalid("invalid export ID"))?;
                a.finish(3)?;
                match verb {
                    "publish" => Action::PublishExport { id },
                    "publication" => Action::GetPublication { id },
                    _ => Action::DiscardExport { id },
                }
            }
            verb @ ("snapshot" | "epochs" | "gc") => {
                let branch = a
                    .take("--branch")
                    .ok_or_else(|| invalid("--branch is required"))?;
                let keep = if verb == "gc" {
                    a.number("--keep", 2)?
                } else {
                    2
                };
                let before = if verb == "epochs" {
                    a.take("--before")
                        .map(|s| s.parse())
                        .transpose()
                        .map_err(|_| invalid("invalid history cursor"))?
                } else {
                    None
                };
                let limit = if verb == "epochs" {
                    a.number("--limit", 100)?
                } else {
                    100
                };
                a.finish(2)?;
                match verb {
                    "snapshot" => Action::CurrentSnapshot { branch },
                    "epochs" => Action::ListSnapshots {
                        branch,
                        before,
                        limit,
                    },
                    _ => Action::CollectSnapshots { branch, keep },
                }
            }
            verb @ ("epoch" | "pin") => {
                let id = a
                    .required(2)?
                    .parse()
                    .map_err(|_| invalid("invalid epoch ID"))?;
                let ttl_ms = if verb == "pin" {
                    a.number("--ttl-ms", 60000)?
                } else {
                    60000
                };
                a.finish(3)?;
                if verb == "epoch" {
                    Action::GetSnapshot { id }
                } else {
                    Action::PinSnapshot { id, ttl_ms }
                }
            }
            verb @ ("renew" | "unpin") => {
                let id = a
                    .required(2)?
                    .parse()
                    .map_err(|_| invalid("invalid lease ID"))?;
                let ttl_ms = if verb == "renew" {
                    a.number("--ttl-ms", 60000)?
                } else {
                    60000
                };
                a.finish(3)?;
                if verb == "renew" {
                    Action::RenewSnapshotLease { id, ttl_ms }
                } else {
                    Action::ReleaseSnapshotLease { id }
                }
            }
            "configure" => {
                let python = a
                    .take("--python")
                    .ok_or_else(|| invalid("--python is required"))?;
                let worker = a
                    .take("--worker")
                    .ok_or_else(|| invalid("--worker is required"))?;
                a.finish(2)?;
                Action::ConfigureAnalytics {
                    python: PathBuf::from(python),
                    worker: PathBuf::from(worker),
                }
            }
            verb @ ("export" | "refresh") => {
                let branch = a
                    .take("--branch")
                    .ok_or_else(|| invalid("--branch is required"))?;
                let key = a
                    .take("--key")
                    .unwrap_or_else(|| OperationId::new().to_string());
                let defaults = crate::store::ExportLimits::default();
                let limits = crate::store::ExportLimits {
                    max_bytes: a.number("--max-bytes", defaults.max_bytes)?,
                    timeout_ms: a.number("--timeout-ms", defaults.timeout_ms)?,
                };
                a.finish(2)?;
                if verb == "refresh" {
                    Action::AnalyticsRefresh {
                        branch,
                        key,
                        limits,
                    }
                } else {
                    Action::Export {
                        branch,
                        key,
                        limits,
                    }
                }
            }
            verb @ ("status" | "cancel") => {
                let id = a
                    .required(2)?
                    .parse()
                    .map_err(|_| invalid("invalid export ID"))?;
                a.finish(3)?;
                if verb == "status" {
                    Action::AnalyticsStatus { id }
                } else {
                    Action::CancelExport { id }
                }
            }
            _ => {
                return Err(invalid("unknown analytics command; use --help"));
            }
        },
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
    let analytical_wait_ms = match &action {
        Action::AnalyticsRefresh { limits, .. } => limits.timeout_ms.saturating_add(60000),
        _ => 600000,
    };
    let result = c.call(action)?;
    if wait && command == "analytics" {
        let id = serde_json::from_value(result["id"].clone())?;
        let action = if matches!(a.pos[1].as_str(), "open" | "close" | "cancel-session") {
            Action::AnalyticsSession { id }
        } else {
            Action::AnalyticsStatus { id }
        };
        let result = wait_analytics(&c, action, analytical_wait_ms)?;
        println!("{result}");
        return Ok(
            if matches!(
                result["state"].as_str(),
                Some("ready" | "published" | "closed")
            ) {
                0
            } else {
                7
            },
        );
    }
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

fn sql_argument(a: &mut Args) -> Result<String> {
    match (a.take("--sql"), a.take("--file")) {
        (Some(sql), None) => Ok(sql),
        (None, Some(file)) => {
            let mut s = String::new();
            std::fs::File::open(file)?
                .take(32769)
                .read_to_string(&mut s)?;
            Ok(s)
        }
        _ => Err(invalid("provide exactly one of --sql or --file")),
    }
}
fn close_analytics(c: &Client, id: OperationId) -> Result<()> {
    c.call(Action::AnalyticsClose { id })?;
    // Auto-owned CLI sessions must release admission before the next command.
    // The API remains asynchronous and retains its reference until worker death.
    let result = wait_analytics(c, Action::AnalyticsSession { id }, 60000)?;
    if !matches!(result["state"].as_str(), Some("closed" | "failed")) {
        return Err(invalid("analytical session cleanup has not completed"));
    }
    Ok(())
}

fn wait_analytics(c: &Client, action: Action, timeout_ms: u64) -> Result<Value> {
    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    let mut previous = String::new();
    loop {
        let value = c.call(action.clone())?;
        let state = value["state"].as_str().unwrap_or("unknown");
        if matches!(
            state,
            "ready" | "published" | "complete" | "failed" | "closed" | "cancelled"
        ) {
            return Ok(value);
        }
        if state != previous {
            eprintln!(
                "{}",
                json!({"id":value["id"],"state":state,"refresh_id":value["refresh_id"]})
            );
            previous = state.into();
        }
        if Instant::now() >= deadline {
            return Err(supabricks_core::error::OperationError::Unavailable(
                "analytical wait deadline; inspect status or cancel the returned ID".into(),
            )
            .into());
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}
