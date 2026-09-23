//! A private PG transaction. Credentials and control connections never cross the API.
use super::{Capability, denied};
use crate::store::Result;
use futures_util::{SinkExt, TryStreamExt};
use serde_json::{Value, json};
use std::{
    sync::mpsc,
    time::{Duration, Instant},
};
use tokio_postgres::{Config, NoTls};

fn pg(e: tokio_postgres::Error) -> crate::store::Error {
    crate::store::error::conflict(format!(
        "governed PostgreSQL rejected request ({})",
        e.code().map(|c| c.code()).unwrap_or("connection")
    ))
}
pub(crate) struct Target {
    pub port: u16,
    pub password: String,
    pub capture_identity: Option<Value>,
}
pub(crate) struct Pending {
    decision: mpsc::Sender<bool>,
    done: mpsc::Receiver<Result<Value>>,
}
impl Pending {
    pub(crate) fn commit(self) -> Result<Value> {
        self.decision.send(true).map_err(|_| denied())?;
        self.done
            .recv_timeout(Duration::from_secs(4))
            .map_err(|_| denied())?
    }
}
struct Connection(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for Connection {
    fn drop(&mut self) {
        self.0.abort();
    }
}
async fn connect(
    target: &Target,
    user: &str,
    password: &str,
) -> Result<(tokio_postgres::Client, Connection)> {
    let options = if user == "cloud_admin" {
        "-c statement_timeout=2000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=5000 -c search_path=pg_catalog -c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1"
    } else {
        "-c statement_timeout=2000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=5000 -c search_path=pg_catalog"
    };
    let (client, connection) = Config::new()
        .host("127.0.0.1")
        .port(target.port)
        .user(user)
        .password(password)
        .dbname("postgres")
        .application_name("supabricks-governed")
        .options(options)
        .connect(NoTls)
        .await
        .map_err(pg)?;
    Ok((client, Connection(tokio::spawn(connection))))
}
// First keyword is only a transaction-control allowlist. PostgreSQL's extended
// protocol enforces one statement, and PG privileges enforce data permissions.
fn statement(cap: Capability, sql: &str) -> Result<()> {
    if sql.len() > 32768 {
        return Err(denied());
    }
    let words: Vec<_> = sql
        .split_whitespace()
        .take(2)
        .map(str::to_ascii_uppercase)
        .collect();
    let first = words.first().map(String::as_str).unwrap_or("");
    let second = words.get(1).map(String::as_str).unwrap_or("");
    let allowed = match cap {
        Capability::Read => matches!(first, "SELECT" | "WITH" | "VALUES" | "TABLE"),
        Capability::Write => matches!(first, "INSERT" | "UPDATE" | "DELETE" | "MERGE" | "WITH"),
        Capability::Ddl => matches!(
            (first, second),
            ("CREATE", "TABLE" | "INDEX" | "UNIQUE")
                | ("ALTER", "TABLE")
                | ("DROP", "TABLE" | "INDEX")
        ),
        _ => false,
    };
    if !allowed {
        return Err(denied());
    }
    Ok(())
}
/// Conservative whole-branch profile: ordinary public tables, no RLS, foreign
/// tables, views, user routines or triggers that can elevate an ordinary role.
async fn profile(c: &tokio_postgres::Client, capture: Option<&Value>) -> Result<()> {
    // Only the durable, owned capture identity can exempt the engine DDL fence.
    // Names alone and user-defined routines never confer this exemption.
    let mut capture_function = 0i64;
    if let Some(identity) = capture {
        let generation: uuid::Uuid = identity["generation"]
            .as_str()
            .ok_or_else(denied)?
            .parse()
            .map_err(|_| denied())?;
        let name = format!("sbcap_{}", generation.simple());
        let owner = identity.to_string();
        let verified = c.query_opt(r#"SELECT p.oid::bigint FROM pg_namespace n JOIN pg_proc p ON p.pronamespace=n.oid
          WHERE n.nspname=$1 AND n.nspowner='cloud_admin'::regrole AND obj_description(n.oid,'pg_namespace')::jsonb=$2::text::jsonb
          AND p.proname='fence' AND p.pronargs=0 AND p.proowner='cloud_admin'::regrole AND p.prosecdef
          AND p.prorettype='event_trigger'::regtype AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
          AND p.proconfig=ARRAY['search_path=pg_catalog']
          AND (SELECT count(*) FROM pg_event_trigger e WHERE e.evtfoid=p.oid AND e.evtenabled='O' AND e.evtowner='cloud_admin'::regrole
               AND ((e.evtname=$1 || '_end' AND e.evtevent='ddl_command_end') OR (e.evtname=$1 || '_drop' AND e.evtevent='sql_drop')))=2"#, &[&name,&owner]).await.map_err(pg)?;
        if let Some(row) = verified {
            capture_function = row.get(0);
        }
    }
    let ok:bool=c.query_one(r#"SELECT
      NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname NOT IN ('information_schema','_supabricks')
          AND NOT (n.nspname IN ('neon','neon_migration') AND c.relowner='cloud_admin'::regrole)
          AND (n.nspname!='public' OR c.relrowsecurity OR c.relforcerowsecurity OR c.relkind NOT IN ('r','p','i','I','S','t')))
      AND NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname NOT IN ('information_schema')
          AND p.oid::bigint!=$1
          AND NOT (n.nspname='neon' AND p.proowner='cloud_admin'::regrole AND p.probin='$libdir/neon' AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='c') AND NOT p.prosecdef))
      AND NOT EXISTS(SELECT 1 FROM pg_trigger WHERE NOT tgisinternal)"#, &[&capture_function]).await.map_err(pg)?.get(0);
    if !ok {
        return Err(crate::store::error::invalid(
            "unsupported whole-branch PostgreSQL profile",
        ));
    }
    Ok(())
}
/// Reconciliation runs on a private destination before any governed query. Old
/// logins lose passwords and sessions; the restricted owner loses memberships.
async fn reconcile(c: &tokio_postgres::Client) -> Result<()> {
    c.batch_execute(r#"DO $$ DECLARE r record; BEGIN
      FOR r IN SELECT rolname FROM pg_roles WHERE rolcanlogin AND rolname!='cloud_admin' LOOP
        EXECUTE format('ALTER ROLE %I NOLOGIN PASSWORD NULL',r.rolname);
        PERFORM pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=r.rolname;
      END LOOP;
    END $$;
    REVOKE ALL ON DATABASE postgres FROM PUBLIC;
    -- SQL is one allowlisted statement. Deny the function form of SET too,
    -- so user expressions cannot remove session/transaction deadlines.
    REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text,text,boolean) FROM PUBLIC;
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
    DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='neon') THEN
      REVOKE ALL ON SCHEMA neon FROM PUBLIC;
    END IF; END $$;
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
    DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='sb_governed_owner') THEN
      CREATE ROLE sb_governed_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF; END $$;
    ALTER ROLE sb_governed_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD NULL;
    ALTER ROLE sb_governed_owner RESET ALL;
    DO $$ DECLARE r record; BEGIN
      FOR r IN SELECT parent.rolname FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid
        WHERE m.member='sb_governed_owner'::regrole LOOP
        EXECUTE format('REVOKE %I FROM sb_governed_owner',r.rolname);
      END LOOP;
      FOR r IN SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname!='information_schema' LOOP
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC',r.nspname);
      END LOOP;
      FOR r IN SELECT c.relname,string_agg(quote_ident(a.attname),',') AS cols
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        WHERE n.nspname='public' AND c.relkind IN ('r','p') GROUP BY c.relname LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES (%s) ON TABLE public.%I FROM PUBLIC',r.cols,r.relname);
      END LOOP;
    END $$;
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public' AND nspowner!='sb_governed_owner'::regrole) THEN
        ALTER SCHEMA public OWNER TO sb_governed_owner;
      END IF;
    END $$;
    DO $$ DECLARE r record; BEGIN
      FOR r IN SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relowner!='sb_governed_owner'::regrole AND c.relname NOT IN ('health_check','health_check_id_seq') AND c.relkind IN ('r','p','S') ORDER BY c.relkind LOOP
        IF r.relkind='S' THEN
          -- Owned sequences follow their table's owner; independent ones can be changed.
          IF NOT EXISTS(SELECT 1 FROM pg_depend d JOIN pg_class c ON c.oid=d.objid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=r.relname AND d.deptype IN ('a','i')) THEN
            EXECUTE format('ALTER SEQUENCE public.%I OWNER TO sb_governed_owner',r.relname);
          END IF;
        ELSE EXECUTE format('ALTER TABLE public.%I OWNER TO sb_governed_owner',r.relname); END IF;
      END LOOP;
    END $$"#).await.map_err(pg)?;
    Ok(())
}
/// Check the source without broad export authority. Called before the exporter
/// receives a ticket; no user SQL or supplied role name is executed as control.
pub(crate) fn inspect(target: Target) -> Result<()> {
    check(target, false)
}
pub(crate) fn sanitize(target: Target) -> Result<()> {
    check(target, true)
}
fn check(target: Target, sanitize: bool) -> Result<()> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(async {
            tokio::time::timeout(Duration::from_secs(4), async {
                let (c, _connection) = connect(&target, "cloud_admin", &target.password).await?;
                profile(&c, target.capture_identity.as_ref()).await?;
                if sanitize {
                    reconcile(&c).await?;
                }
                Ok(())
            })
            .await
            .map_err(|_| denied())?
        })
}
pub(crate) fn prepare(
    target: Target,
    actor: String,
    cap: Capability,
    sql: String,
    expires_ms: i64,
) -> Result<Pending> {
    statement(cap, &sql)?;
    prepare_work(target, actor, cap, Work::Sql(sql), expires_ms)
}
enum Work {
    Export(
        crate::projects::data::Selection,
        crate::projects::data::Provenance,
    ),
    Sql(String),
    Import(crate::projects::data::Verified),
}
pub(crate) fn prepare_import(
    target: Target,
    actor: String,
    archive_hex: String,
    expires_ms: i64,
) -> Result<Pending> {
    if archive_hex.len() > 60000 {
        return Err(denied());
    }
    let bytes = hex::decode(archive_hex).map_err(|_| denied())?;
    let archive = crate::projects::data::decode(&bytes)?;
    if archive
        .content
        .tables
        .iter()
        .any(|t| t.table.schema != "public")
    {
        return Err(denied());
    }
    prepare_work(
        target,
        actor,
        Capability::Ddl,
        Work::Import(archive),
        expires_ms,
    )
}
pub(crate) fn prepare_export(
    target: Target,
    actor: String,
    selection: crate::projects::data::Selection,
    source: crate::projects::data::Provenance,
    expires_ms: i64,
) -> Result<Pending> {
    selection.validate()?;
    if selection.tables.iter().any(|t| t.schema != "public") {
        return Err(denied());
    }
    prepare_work(
        target,
        actor,
        Capability::Read,
        Work::Export(selection, source),
        expires_ms,
    )
}
fn prepare_work(
    target: Target,
    actor: String,
    cap: Capability,
    work: Work,
    expires_ms: i64,
) -> Result<Pending> {
    let (decision, decisions) = mpsc::channel();
    let (ready_tx, ready) = mpsc::sync_channel(1);
    let (done_tx, done) = mpsc::sync_channel(1);
    // Names contain only generated hex, never an identity label or SQL input.
    let user = format!(
        "sbg_{}_{}",
        &crate::identity::hash(&actor)[..12],
        uuid::Uuid::new_v4().simple()
    );
    let password = uuid::Uuid::new_v4().simple().to_string();
    std::thread::Builder::new().name("governed-pg".into()).spawn(move || {
        let result=(|| {
            let rt=tokio::runtime::Builder::new_current_thread().enable_all().build()?;
            rt.block_on(async {
                let deadline=Instant::now()+Duration::from_secs(10);
                let(c,_control)=tokio::time::timeout(Duration::from_secs(3),connect(&target,"cloud_admin",&target.password)).await.map_err(|_|denied())??;
                let result=tokio::time::timeout(Duration::from_secs(10),async {
                    profile(&c, target.capture_identity.as_ref()).await?;
                    reconcile(&c).await?;
                    let expiry=chrono::DateTime::from_timestamp_millis(expires_ms.min(crate::identity::now()+10000)).ok_or_else(denied)?.to_rfc3339();
                    let hash=supabricks_core::keys::pg_md5(&password,&user);
                    c.batch_execute(&format!("CREATE ROLE {user} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD 'md5{hash}' VALID UNTIL '{expiry}'; ALTER ROLE {user} SET transaction_timeout=10000; ALTER ROLE {user} SET log_statement=none; ALTER ROLE {user} SET log_min_error_statement=panic; ALTER ROLE {user} SET log_min_duration_statement=-1; GRANT CONNECT ON DATABASE postgres TO {user}; GRANT USAGE ON SCHEMA public TO {user};")).await.map_err(pg)?;
                    let grants=match cap {
                        Capability::Read=>format!("DO $$ DECLARE r record; BEGIN FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relname!='health_check' LOOP EXECUTE format('GRANT SELECT ON TABLE public.%I TO {user}',r.relname); END LOOP; END $$"),
                        Capability::Write=>format!("DO $$ DECLARE r record; BEGIN FOR r IN SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','S') AND c.relname NOT IN ('health_check','health_check_id_seq') LOOP IF r.relkind='S' THEN EXECUTE format('GRANT USAGE,SELECT ON SEQUENCE public.%I TO {user}',r.relname); ELSE EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON TABLE public.%I TO {user}',r.relname); END IF; END LOOP; END $$"),
                        Capability::Ddl=>format!("GRANT sb_governed_owner TO {user} WITH INHERIT FALSE, SET TRUE"),
                        _=>return Err(denied()),
                    };
                    c.batch_execute(&grants).await.map_err(pg)?;
                    let(mut client,_session)=connect(&target,&user,&password).await?;
                    let tx=client.transaction().await.map_err(pg)?;
                    tx.batch_execute("SET LOCAL search_path=public,pg_catalog; SET LOCAL row_security=off; SET LOCAL DateStyle='ISO, YMD'; SET LOCAL IntervalStyle='postgres'; SET LOCAL TimeZone='UTC'; SET LOCAL extra_float_digits=3; SET LOCAL bytea_output='hex'").await.map_err(pg)?;
                    if cap==Capability::Ddl {tx.batch_execute("SET LOCAL ROLE sb_governed_owner").await.map_err(pg)?;}
                    tx.batch_execute(if cap==Capability::Read {"SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"} else {"SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"}).await.map_err(pg)?;
                    let snapshot:String=tx.query_one("SELECT pg_current_snapshot()::text",&[]).await.map_err(pg)?.get(0);
                    let mut value=if let Work::Export(selection,source)=&work {
                        export(&tx,selection,source.clone()).await?
                    } else if let Work::Import(archive)=&work {
                        import(&tx,archive).await?
                    } else if let Work::Sql(sql)=&work { if cap==Capability::Read {
                        let sql=format!("SELECT left(row_to_json(q)::text,32769) FROM ({}) q LIMIT 201",sql.trim().trim_end_matches(';'));
                        let stmt=tx.prepare(&sql).await.map_err(pg)?;
                        let rows=tx.query_raw(&stmt,std::iter::empty::<&str>()).await.map_err(pg)?;
                        tokio::pin!(rows);
                        let mut values=Vec::new();let mut bytes=0;
                        while let Some(row)=rows.try_next().await.map_err(pg)? {
                            let text:String=row.try_get::<_,Option<String>>(0).map_err(pg)?.ok_or_else(denied)?;bytes+=text.len();
                            if values.len()>=200 || bytes>32768 {return Err(denied());}
                            let value=serde_json::from_str::<Value>(&text)?;
                            if !value.is_object() {return Err(denied());}
                            values.push(value);
                        }
                        json!({"rows":values})
                    } else {
                        let stmt=tx.prepare(&sql).await.map_err(pg)?;
                        let count=tx.execute(&stmt,&[]).await.map_err(pg)?;
                        json!({"affected":count})
                    }} else {return Err(denied());};
                    let transaction:Option<String>=tx.query_one("SELECT pg_current_xact_id_if_assigned()::text",&[]).await.map_err(pg)?.get(0);
                    value["data_revision"]=json!({"pg_snapshot":snapshot,"pg_transaction":transaction});
                    ready_tx.send(Ok(())).map_err(|_|denied())?;
                    // The sole writer rechecks session, branch and exact policy.
                    // Dropping Pending or writer failure means rollback.
                    let remaining=deadline.saturating_duration_since(Instant::now()).min(Duration::from_secs(3));
                    if decisions.recv_timeout(remaining).unwrap_or(false) && crate::identity::now()<expires_ms {
                        tx.commit().await.map_err(pg)?;
                        Ok(value)
                    } else {Err(denied())}
                }).await.map_err(|_|denied()).and_then(|v|v);
                // VALID UNTIL does not kill sessions. Explicitly terminate even
                // after an error, timeout or dropped request before dropping role.
                let _=tokio::time::timeout(Duration::from_secs(3),async {
                    c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=$1", &[&user]).await.ok();
                    c.batch_execute(&format!("DROP OWNED BY {user}; DROP ROLE IF EXISTS {user}")).await.ok();
                }).await;
                result
            })
        })();
        match result {
            Ok(value)=>{let _=done_tx.send(Ok(value));},
            Err(error)=>{let _=ready_tx.try_send(Err(error));let _=done_tx.send(Err(denied()));}
        }
    })?;
    ready
        .recv_timeout(Duration::from_secs(10))
        .map_err(|_| denied())??;
    Ok(Pending { decision, done })
}

// Reuses the .sbdata validator and non-executable table description. Table-set
// creation and COPY stay in the same uncommitted transaction as the policy fence.
async fn import(
    tx: &tokio_postgres::Transaction<'_>,
    archive: &crate::projects::data::Verified,
) -> Result<Value> {
    use crate::projects::data::{ConstraintKind, Locale, quote};
    let row=tx.query_one("SELECT datlocprovider::text,datcollate,datctype,datlocale,pg_database_collation_actual_version(oid) FROM pg_database WHERE datname=current_database()",&[]).await.map_err(pg)?;
    let locale = Locale {
        provider: row.get(0),
        collate: row.get(1),
        ctype: row.get(2),
        locale: row.get(3),
        version: row.get(4),
    };
    if archive.content.requires_matching_locale() && locale != archive.content.locale {
        return Err(denied());
    }
    let mut tables = Vec::new();
    for table in &archive.content.tables {
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
        for constraint in &table.constraints {
            definitions.push(format!(
                "CONSTRAINT {} {} ({})",
                quote(&constraint.name),
                match constraint.kind {
                    ConstraintKind::PrimaryKey => "PRIMARY KEY",
                    ConstraintKind::Unique => "UNIQUE",
                },
                constraint
                    .columns
                    .iter()
                    .map(|c| quote(c))
                    .collect::<Vec<_>>()
                    .join(",")
            ));
        }
        tx.batch_execute(&format!(
            "CREATE TABLE {} ({})",
            table.table.sql(),
            definitions.join(",")
        ))
        .await
        .map_err(pg)?;
        let columns = table
            .columns
            .iter()
            .map(|c| quote(&c.name))
            .collect::<Vec<_>>()
            .join(",");
        let sink = tx
            .copy_in::<_, std::io::Cursor<Vec<u8>>>(&format!(
                "COPY {} ({columns}) FROM STDIN WITH (FORMAT text)",
                table.table.sql()
            ))
            .await
            .map_err(pg)?;
        tokio::pin!(sink);
        for chunk in table.copy_text.as_bytes().chunks(65536) {
            sink.as_mut()
                .send(std::io::Cursor::new(chunk.to_vec()))
                .await
                .map_err(pg)?;
        }
        let rows = sink.as_mut().finish().await.map_err(pg)?;
        if rows != table.rows {
            return Err(denied());
        }
        tables.push(json!({"table":table.table,"rows":rows}));
    }
    Ok(
        json!({"tables":tables,"archive_sha256":archive.archive_sha256,"content_sha256":archive.content_sha256}),
    )
}

async fn export(
    tx: &tokio_postgres::Transaction<'_>,
    selection: &crate::projects::data::Selection,
    mut source: crate::projects::data::Provenance,
) -> Result<Value> {
    use crate::projects::data::{self, Content, Locale, quote};
    tx.batch_execute("SET LOCAL DateStyle='ISO, YMD'; SET LOCAL IntervalStyle='postgres'; SET LOCAL TimeZone='UTC'; SET LOCAL extra_float_digits=3; SET LOCAL bytea_output='hex'").await.map_err(pg)?;
    for table in &selection.tables {
        tx.batch_execute(&format!("LOCK TABLE {} IN ACCESS SHARE MODE", table.sql()))
            .await
            .map_err(pg)?;
    }
    let row=tx.query_one("SELECT datlocprovider::text,datcollate,datctype,datlocale,pg_database_collation_actual_version(oid) FROM pg_database WHERE datname=current_database()",&[]).await.map_err(pg)?;
    let locale = Locale {
        provider: row.get(0),
        collate: row.get(1),
        ctype: row.get(2),
        locale: row.get(3),
        version: row.get(4),
    };
    source.snapshot = tx
        .query_one("SELECT pg_current_snapshot()::text", &[])
        .await
        .map_err(pg)?
        .get(0);
    let mut tables = Vec::new();
    let mut total = 0;
    for name in &selection.tables {
        let mut table = data::postgres::describe_governed(tx, name.clone(), false).await?;
        let columns = table
            .columns
            .iter()
            .map(|c| quote(&c.name))
            .collect::<Vec<_>>()
            .join(",");
        let stream = tx
            .copy_out(&format!(
                "COPY {} ({columns}) TO STDOUT WITH (FORMAT text)",
                table.table.sql()
            ))
            .await
            .map_err(pg)?;
        tokio::pin!(stream);
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.try_next().await.map_err(pg)? {
            total += chunk.len();
            if total > 24000 {
                return Err(denied());
            }
            bytes.extend_from_slice(&chunk);
        }
        table.rows = bytes.iter().filter(|b| **b == b'\n').count() as u64;
        table.data_sha256 = data::hash(&bytes);
        table.copy_text = String::from_utf8(bytes).map_err(|_| denied())?;
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
    let bytes = data::encode(content)?;
    if bytes.len() > 30000 {
        return Err(denied());
    }
    Ok(
        json!({"format":"sbdata","archive_sha256":data::hash(&bytes),"archive_hex":hex::encode(bytes)}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sql_transaction_and_privilege_control_are_never_user_statements() {
        for cap in [Capability::Read, Capability::Write, Capability::Ddl] {
            for sql in [
                "COMMIT",
                "ROLLBACK",
                "BEGIN",
                "SET ROLE cloud_admin",
                "DO $$ BEGIN END $$",
                "CREATE ROLE stolen",
                "ALTER ROLE sb_governed_owner LOGIN",
                "GRANT sb_governed_owner TO PUBLIC",
                "COPY t TO PROGRAM 'true'",
                "VACUUM",
                "/* hidden */ COMMIT",
            ] {
                assert!(statement(cap, sql).is_err(), "{cap:?} {sql}");
            }
        }
        assert!(statement(Capability::Read, "SELECT 42").is_ok());
        assert!(statement(Capability::Write, "INSERT INTO t VALUES(42)").is_ok());
        assert!(statement(Capability::Ddl, "CREATE TABLE t(id int)").is_ok());
    }
}
