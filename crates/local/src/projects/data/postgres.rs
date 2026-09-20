use super::*;
use crate::{api::Action, client::Client as LocalClient, query::FramedSocket, store::Error};
use futures_util::{SinkExt, TryStreamExt};
use std::{future::Future, time::Duration};
use tokio_postgres::{Client, Config, NoTls};

const SETTINGS: &str = "SET LOCAL search_path=pg_catalog; SET LOCAL DateStyle='ISO, YMD'; SET LOCAL IntervalStyle='postgres'; SET LOCAL TimeZone='UTC'; SET LOCAL extra_float_digits=3; SET LOCAL bytea_output='hex'; SET LOCAL row_security=off;";
const RECEIPT: &str = "_supabricks.logical_import_receipts_v1";

fn db(error: tokio_postgres::Error) -> Error {
    // Never put source rows, credentials or server DETAIL in a public report.
    if let Some(e) = error.as_db_error() {
        supabricks_core::error::OperationError::Query {
            sqlstate: e.code().code().into(),
            message: "logical data operation rejected by PostgreSQL".into(),
        }
        .into()
    } else {
        conflict("logical data connection interrupted; inspect import status before retrying")
    }
}
fn run<T>(future: impl Future<Output = Result<T>>) -> Result<T> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(async {
            tokio::time::timeout(Duration::from_secs(TIMEOUT_SECONDS), future)
                .await
                .map_err(|_| {
                    conflict(
                        "logical data deadline exceeded; inspect import status before retrying",
                    )
                })?
        })
}
struct Connection(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for Connection {
    fn drop(&mut self) {
        self.0.abort();
    }
}
async fn connect(target: &Value) -> Result<(Client, Connection)> {
    if target["host"] != "127.0.0.1" {
        return Err(invalid("logical data requires the local gateway"));
    }
    let port = target["port"]
        .as_u64()
        .filter(|p| *p > 0 && *p <= 65535)
        .ok_or_else(|| invalid("missing gateway port"))? as u16;
    let socket = tokio::net::TcpStream::connect(("127.0.0.1", port)).await?;
    let (client,connection)=Config::new().user("supabricks_owner").password(target["password"].as_str().ok_or_else(||invalid("missing app credential"))?)
        .dbname("postgres").application_name("supabricks-logical-data")
        .options("-c statement_timeout=30000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000")
        .connect_raw(FramedSocket::new(socket),NoTls).await.map_err(db)?;
    Ok((client, Connection(tokio::spawn(connection))))
}
async fn version(client: &Client) -> Result<()> {
    let version: String = client
        .query_one("SHOW server_version_num", &[])
        .await
        .map_err(db)?
        .get(0);
    if version
        .parse::<u32>()
        .map_err(|_| invalid("invalid PostgreSQL version"))?
        / 10000
        != 17
    {
        return Err(invalid("logical data v1 requires PostgreSQL 17"));
    }
    Ok(())
}
async fn locale(client: &Client) -> Result<Locale> {
    let row=client.query_one("SELECT datlocprovider::text,datcollate,datctype,datlocale,pg_database_collation_actual_version(oid),datcollversion,pg_encoding_to_char(encoding) FROM pg_database WHERE datname=current_database()",&[]).await.map_err(db)?;
    let result = Locale {
        provider: row.get(0),
        collate: row.get(1),
        ctype: row.get(2),
        locale: row.get(3),
        version: row.get(4),
    };
    result.validate()?;
    if row.get::<_, String>(6) != "UTF8" || row.get::<_, Option<String>>(5) != result.version {
        return Err(invalid(
            "database encoding or collation version is incompatible",
        ));
    }
    Ok(result)
}
pub fn export(
    local: &LocalClient,
    branch: &str,
    selection: Selection,
    output: &Path,
) -> Result<Value> {
    selection.validate()?;
    // Check output before starting a database snapshot; final publication still
    // uses the descriptor-relative no-replace operation.
    let publication = super::super::publication::Publication::new(output)?;
    let inspection = crate::projects::inspect(&local.binding.worktree, None)?;
    let state = local.call(Action::GetBranch {
        branch: branch.into(),
    })?;
    let target = local.call(Action::Connect {
        branch: Some(branch.into()),
    })?;
    let source = Provenance {
        definition_id: inspection.definition.id,
        source_sha256: inspection.source_sha256,
        runtime_project_id: local.binding.project_id,
        branch_id: serde_json::from_value(target["branch_id"].clone())?,
        branch_revision: state["revision"]
            .as_i64()
            .ok_or_else(|| invalid("missing branch revision"))?,
        snapshot: String::new(),
    };
    let content = run(export_tables(&target, selection, source))?;
    let current = local.call(Action::GetBranch {
        branch: branch.into(),
    })?;
    if current["revision"] != state["revision"]
        || current["branch"]["id"] != target["branch_id"]
        || crate::projects::inspect(&local.binding.worktree, None)?.source_sha256
            != content.source.source_sha256
    {
        return Err(conflict(
            "source binding, branch revision or source definition changed during export",
        ));
    }
    let bytes = encode(content)?;
    let verified = decode(&bytes)?;
    publication.write("data.sbdata", &bytes)?;
    publication.publish_file("data.sbdata")?;
    Ok(verified.report())
}
async fn export_tables(
    target: &Value,
    mut selection: Selection,
    mut source: Provenance,
) -> Result<Content> {
    let (client, _connection) = connect(target).await?;
    client
        .batch_execute(&format!(
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; {SETTINGS}"
        ))
        .await
        .map_err(db)?;
    selection.tables.sort();
    // Lock all relations before reading schema or rows. One live MVCC snapshot
    // protects the whole set; no Delta epochs or unpinned files are inputs.
    for table in &selection.tables {
        client
            .batch_execute(&format!("LOCK TABLE {} IN ACCESS SHARE MODE", table.sql()))
            .await
            .map_err(db)?;
    }
    version(&client).await?;
    let locale = locale(&client).await?;
    source.snapshot = client
        .query_one("SELECT pg_current_snapshot()::text", &[])
        .await
        .map_err(db)?
        .get(0);
    let mut tables = Vec::new();
    let mut total = 0usize;
    let mut all_rows = 0u64;
    for name in selection.tables {
        let mut table = describe(&client, name).await?;
        let columns = table
            .columns
            .iter()
            .map(|c| quote(&c.name))
            .collect::<Vec<_>>()
            .join(",");
        let stream = client
            .copy_out(&format!(
                "COPY {} ({columns}) TO STDOUT WITH (FORMAT text)",
                table.table.sql()
            ))
            .await
            .map_err(db)?;
        futures_util::pin_mut!(stream);
        let mut bytes = Vec::new();
        let mut row_bytes = 0usize;
        while let Some(chunk) = stream.try_next().await.map_err(db)? {
            total = total
                .checked_add(chunk.len())
                .ok_or_else(|| invalid("data size overflow"))?;
            if total > MAX_DATA {
                return Err(invalid("logical data exceeds 32 MiB"));
            }
            for byte in &chunk {
                row_bytes += 1;
                if row_bytes > MAX_ROW {
                    return Err(invalid("logical data row exceeds 256 KiB"));
                }
                if *byte == b'\n' {
                    row_bytes = 0;
                    table.rows += 1;
                    all_rows += 1;
                }
            }
            if all_rows > MAX_ROWS {
                return Err(invalid("logical data exceeds 100000 rows"));
            }
            bytes.extend_from_slice(&chunk);
        }
        table.data_sha256 = hash(&bytes);
        table.copy_text =
            String::from_utf8(bytes).map_err(|_| invalid("logical data requires UTF-8"))?;
        tables.push(table);
    }
    let content = Content {
        format_version: 1,
        profile: "postgres_tables".into(),
        postgres_major: 17,
        locale,
        source,
        tables,
    };
    content.validate()?;
    client.batch_execute("COMMIT").await.map_err(db)?;
    Ok(content)
}
fn data_type(oid: u32, modifier: i32) -> Result<Type> {
    let plain = match oid {
        16 => Type::Boolean,
        21 => Type::Smallint,
        23 => Type::Integer,
        20 => Type::Bigint,
        25 => Type::Text,
        17 => Type::Bytea,
        2950 => Type::Uuid,
        114 => Type::Json,
        3802 => Type::Jsonb,
        700 => Type::Real,
        701 => Type::Double,
        1082 => Type::Date,
        1114 => Type::Timestamp,
        1184 => Type::TimestampTz,
        1043 => {
            return Ok(Type::Varchar {
                length: if modifier == -1 {
                    None
                } else {
                    Some(
                        modifier
                            .checked_sub(4)
                            .filter(|n| *n > 0)
                            .ok_or_else(|| invalid("unsupported varchar modifier"))?
                            as u32,
                    )
                },
            });
        }
        1700 => {
            return if modifier == -1 {
                Ok(Type::Numeric {
                    precision: None,
                    scale: None,
                })
            } else if modifier >= 4 {
                let m = modifier - 4;
                let scale = (m & 2047) as i16;
                Ok(Type::Numeric {
                    precision: Some(((m >> 16) & 65535) as u16),
                    scale: Some(if scale >= 1024 { scale - 2048 } else { scale }),
                })
            } else {
                Err(invalid("unsupported numeric modifier"))
            };
        }
        _ => {
            return Err(invalid(
                "unsupported PostgreSQL data type; logical data export refuses lossy conversion",
            ));
        }
    };
    if modifier != -1 {
        return Err(invalid("unsupported PostgreSQL type modifier"));
    }
    Ok(plain)
}
async fn describe(client: &Client, name: TableName) -> Result<Table> {
    let row=client.query_one("SELECT c.oid,c.relkind::text,c.relpersistence::text,c.relrowsecurity,c.relforcerowsecurity,c.relispartition,c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user),c.reloptions IS NOT NULL,c.reltablespace<>0,EXISTS(SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid),EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'),EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal),EXISTS(SELECT 1 FROM pg_rewrite r WHERE r.ev_class=c.oid),c.relam<>(SELECT oid FROM pg_am WHERE amname='heap') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname=$2",&[&name.schema,&name.name]).await.map_err(db)?;
    if row.get::<_, String>(1) != "r"
        || row.get::<_, String>(2) != "p"
        || !row.get::<_, bool>(6)
        || [3, 4, 5, 7, 8, 9, 10, 11, 12, 13]
            .iter()
            .any(|i| row.get::<_, bool>(*i))
    {
        return Err(invalid(
            "only owner-owned ordinary permanent tables without RLS, inheritance, extensions, triggers, rules or storage options are supported",
        ));
    }
    let oid: u32 = row.get(0);
    let attrs=client.query("SELECT a.attname,a.atttypid,a.atttypmod,a.attnotnull,a.atthasdef,a.attidentity::text,a.attgenerated::text,a.attcollation NOT IN (0,100),a.attisdropped FROM pg_attribute a WHERE a.attrelid=$1 AND a.attnum>0 ORDER BY a.attnum LIMIT 65",&[&oid]).await.map_err(db)?;
    if attrs.is_empty() || attrs.len() > MAX_COLUMNS {
        return Err(invalid("logical data supports 1–64 columns"));
    }
    let mut columns = Vec::new();
    for a in attrs {
        if a.get::<_, bool>(4)
            || !a.get::<_, String>(5).is_empty()
            || !a.get::<_, String>(6).is_empty()
            || a.get::<_, bool>(7)
            || a.get::<_, bool>(8)
        {
            return Err(invalid(
                "defaults, sequences/identity, generated/dropped columns and explicit collations are unsupported",
            ));
        }
        let data_type = data_type(a.get(1), a.get(2))?;
        data_type.sql()?;
        columns.push(Column {
            name: a.get(0),
            data_type,
            nullable: !a.get::<_, bool>(3),
        });
    }
    let raw=client.query("SELECT c.conname,c.contype::text,c.condeferrable,c.convalidated,ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ord) JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum ORDER BY ord) FROM pg_constraint c WHERE c.conrelid=$1 ORDER BY c.conname LIMIT 65",&[&oid]).await.map_err(db)?;
    if raw.len() > 64 {
        return Err(invalid("too many logical data constraints"));
    }
    let mut constraints = Vec::new();
    for c in raw {
        let kind = match c.get::<_, String>(1).as_str() {
            "p" => ConstraintKind::PrimaryKey,
            "u" => ConstraintKind::Unique,
            _ => {
                return Err(invalid(
                    "only NOT NULL, primary key and ordinary UNIQUE constraints are supported",
                ));
            }
        };
        if c.get::<_, bool>(2) || !c.get::<_, bool>(3) {
            return Err(invalid("deferred/unvalidated constraints are unsupported"));
        }
        constraints.push(Constraint {
            name: c.get(0),
            kind,
            columns: c.get(4),
        });
    }
    let unsupported:bool=client.query_one("SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam WHERE i.indrelid=$1 AND (NOT EXISTS(SELECT 1 FROM pg_constraint k WHERE k.conrelid=i.indrelid AND k.conindid=i.indexrelid AND k.contype IN ('p','u')) OR am.amname<>'btree' OR NOT i.indisvalid OR NOT i.indisready OR i.indnullsnotdistinct OR i.indpred IS NOT NULL OR i.indexprs IS NOT NULL OR i.indnkeyatts<>i.indnatts OR c.reloptions IS NOT NULL OR c.reltablespace<>0 OR EXISTS(SELECT 1 FROM unnest(i.indoption) o WHERE o<>0) OR EXISTS(SELECT 1 FROM unnest(i.indclass) op JOIN pg_opclass p ON p.oid=op WHERE NOT p.opcdefault OR p.opcnamespace<>'pg_catalog'::regnamespace) OR EXISTS(SELECT 1 FROM unnest(i.indkey::smallint[],i.indcollation::oid[]) k(attnum,collid) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE a.attcollation<>k.collid)))",&[&oid]).await.map_err(db)?.get(0);
    if unsupported {
        return Err(invalid(
            "standalone/custom indexes and non-default unique semantics are unsupported",
        ));
    }
    Ok(Table {
        table: name,
        columns,
        constraints,
        rows: 0,
        data_sha256: hash(b""),
        copy_text: String::new(),
    })
}

fn key_valid(key: &str) -> Result<()> {
    if key.is_empty() || key.len() > 128 || key.contains('\0') {
        Err(invalid("import key requires 1–128 bytes without NUL"))
    } else {
        Ok(())
    }
}
pub fn import(local: &LocalClient, branch: &str, key: &str, archive: Verified) -> Result<Value> {
    key_valid(key)?;
    archive.content.validate()?;
    let target = local.call(Action::Connect {
        branch: Some(branch.into()),
    })?;
    run(import_tables(
        &target,
        &local.binding.project_id.to_string(),
        key,
        archive,
    ))
}
pub fn status(local: &LocalClient, branch: &str, key: &str) -> Result<Value> {
    key_valid(key)?;
    let target = local.call(Action::Connect {
        branch: Some(branch.into()),
    })?;
    run(async {
        let (client, _connection) = connect(&target).await?;
        let exists: bool = client
            .query_one(
                "SELECT to_regclass('_supabricks.logical_import_receipts_v1') IS NOT NULL",
                &[],
            )
            .await
            .map_err(db)?
            .get(0);
        if !exists {
            return Ok(json!({"api_version":1,"state":"not_committed","key":key}));
        }
        validate_receipt(&client).await?;
        Ok(receipt(
            &client,
            &local.binding.project_id.to_string(),
            target["branch_id"]
                .as_str()
                .ok_or_else(|| invalid("missing branch ID"))?,
            key,
        )
        .await?
        .unwrap_or(json!({"api_version":1,"state":"not_committed","key":key})))
    })
}
async fn receipt(client: &Client, project: &str, branch: &str, key: &str) -> Result<Option<Value>> {
    let row=client.query_opt(&format!("SELECT report FROM {RECEIPT} WHERE project_id=$1 AND branch_id=$2 AND import_key=$3"),&[&project,&branch,&key]).await.map_err(db)?;
    row.map(|r| {
        serde_json::from_str::<Value>(&r.get::<_, String>(0))
            .map_err(|_| conflict("invalid logical import receipt"))
    })
    .transpose()
}
async fn validate_receipt(client: &Client) -> Result<()> {
    let valid:bool=client.query_one("SELECT c.relkind='r' AND c.relpersistence='p' AND NOT c.relrowsecurity AND NOT c.relforcerowsecurity AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user) AND NOT EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid) AND NOT EXISTS(SELECT 1 FROM pg_rewrite r WHERE r.ev_class=c.oid) AND (SELECT array_agg(a.attname::text||':'||a.atttypid::text||':'||a.attnotnull::text ORDER BY a.attnum) FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0)=ARRAY['project_id:25:true','branch_id:25:true','import_key:25:true','report:25:true'] AND (SELECT count(*) FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype='p' AND k.conkey=ARRAY[1,2,3]::smallint[])=1 FROM pg_class c WHERE c.oid='_supabricks.logical_import_receipts_v1'::regclass",&[]).await.map_err(db)?.get(0);
    if !valid {
        return Err(conflict(
            "logical import receipt schema differs; no automatic repair",
        ));
    }
    Ok(())
}
async fn import_tables(
    target: &Value,
    project: &str,
    key: &str,
    archive: Verified,
) -> Result<Value> {
    let branch = target["branch_id"]
        .as_str()
        .ok_or_else(|| invalid("missing branch ID"))?;
    let (client, _connection) = connect(target).await?;
    client
        .batch_execute(&format!("BEGIN; {SETTINGS}"))
        .await
        .map_err(db)?;
    version(&client).await?;
    let destination_locale = locale(&client).await?;
    if archive.content.requires_matching_locale() && destination_locale != archive.content.locale {
        return Err(invalid(
            "text/varchar columns require matching source and destination database locale/version",
        ));
    }
    // Serialize PK08 imports per destination database. PostgreSQL owns both the
    // table-set publication and durable receipt, including lost COMMIT replies.
    client
        .query_one("SELECT pg_advisory_xact_lock(1936745057,8)", &[])
        .await
        .map_err(db)?;
    client.batch_execute(&format!("CREATE SCHEMA IF NOT EXISTS _supabricks; REVOKE ALL ON SCHEMA _supabricks FROM PUBLIC; CREATE TABLE IF NOT EXISTS {RECEIPT}(project_id text NOT NULL,branch_id text NOT NULL,import_key text NOT NULL,report text NOT NULL,PRIMARY KEY(project_id,branch_id,import_key)); REVOKE ALL ON {RECEIPT} FROM PUBLIC;")).await.map_err(db)?;
    validate_receipt(&client).await?;
    if let Some(mut report) = receipt(&client, project, branch, key).await? {
        if report["archive_sha256"] != archive.archive_sha256 {
            return Err(conflict(
                "import key already belongs to different archive bytes",
            ));
        }
        for table in report["tables"]
            .as_array()
            .ok_or_else(|| conflict("invalid import receipt"))?
        {
            let name: TableName = serde_json::from_value(table["table"].clone())?;
            let current: Option<u32> = client
                .query_one("SELECT to_regclass($1)::oid", &[&name.sql()])
                .await
                .map_err(db)?
                .get(0);
            if current.map(u64::from) != table["destination_oid"].as_u64() {
                return Err(conflict(
                    "previously imported table was removed or replaced",
                ));
            }
        }
        client.batch_execute("COMMIT").await.map_err(db)?;
        report["replayed"] = json!(true);
        return Ok(report);
    }
    for table in &archive.content.tables {
        let exists: bool = client
            .query_one("SELECT to_regclass($1) IS NOT NULL", &[&table.table.sql()])
            .await
            .map_err(db)?
            .get(0);
        if exists {
            return Err(conflict(
                "logical import requires fresh table names; existing objects are never replaced",
            ));
        }
    }
    let mut tables = Vec::new();
    for table in &archive.content.tables {
        client
            .batch_execute(&format!(
                "CREATE SCHEMA IF NOT EXISTS {}",
                quote(&table.table.schema)
            ))
            .await
            .map_err(db)?;
        let mut definitions = table
            .columns
            .iter()
            .map(|c| {
                Ok(format!(
                    "{} {}{}",
                    quote(&c.name),
                    c.data_type.sql()?,
                    if c.nullable { "" } else { " NOT NULL" }
                ))
            })
            .collect::<Result<Vec<_>>>()?;
        for c in &table.constraints {
            definitions.push(format!(
                "CONSTRAINT {} {} ({})",
                quote(&c.name),
                match c.kind {
                    ConstraintKind::PrimaryKey => "PRIMARY KEY",
                    ConstraintKind::Unique => "UNIQUE",
                },
                c.columns
                    .iter()
                    .map(|n| quote(n))
                    .collect::<Vec<_>>()
                    .join(",")
            ));
        }
        client
            .batch_execute(&format!(
                "CREATE TABLE {} ({})",
                table.table.sql(),
                definitions.join(",")
            ))
            .await
            .map_err(db)?;
        let columns = table
            .columns
            .iter()
            .map(|c| quote(&c.name))
            .collect::<Vec<_>>()
            .join(",");
        let sink = client
            .copy_in::<_, std::io::Cursor<Vec<u8>>>(&format!(
                "COPY {} ({columns}) FROM STDIN WITH (FORMAT text)",
                table.table.sql()
            ))
            .await
            .map_err(db)?;
        futures_util::pin_mut!(sink);
        for chunk in table.copy_text.as_bytes().chunks(64 * 1024) {
            sink.as_mut()
                .send(std::io::Cursor::new(chunk.to_vec()))
                .await
                .map_err(db)?;
        }
        let count = sink.as_mut().finish().await.map_err(db)?;
        if count != table.rows {
            return Err(conflict(
                "imported row count differs; entire table set rolled back",
            ));
        }
        let oid: u32 = client
            .query_one("SELECT $1::text::regclass::oid", &[&table.table.sql()])
            .await
            .map_err(db)?
            .get(0);
        tables.push(json!({"table":table.table,"rows":count,"destination_oid":oid}));
    }
    let report = json!({"api_version":1,"state":"committed","key":key,"archive_sha256":archive.archive_sha256,"content_sha256":archive.content_sha256,"source":archive.content.source,"destination_project_id":project,"destination_branch_id":branch,"tables":tables,"replayed":false});
    client
        .execute(
            &format!("INSERT INTO {RECEIPT} VALUES($1,$2,$3,$4)"),
            &[&project, &branch, &key, &serde_json::to_string(&report)?],
        )
        .await
        .map_err(db)?;
    checkpoint("before_commit");
    client.batch_execute("COMMIT").await.map_err(db)?;
    checkpoint("after_commit");
    Ok(report)
}

fn checkpoint(name: &str) {
    if std::env::var("SUPABRICKS_TEST_LOGICAL_DATA_KILL")
        .ok()
        .as_deref()
        == Some(name)
    {
        unsafe {
            libc::raise(libc::SIGKILL);
        }
    }
}
