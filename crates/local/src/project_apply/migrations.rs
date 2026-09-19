//! One bounded PostgreSQL statement per migration, with its receipt in the same transaction.
use crate::store::{
    Result,
    error::{conflict, invalid},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, thread::JoinHandle, time::Duration};
use tokio_postgres::{Config, NoTls};

pub fn validate(bytes: &[u8]) -> Result<()> {
    let sql = std::str::from_utf8(bytes).map_err(|_| invalid("migration must be UTF-8"))?;
    if sql.is_empty() || sql.len() > 32768 || sql.contains('\0') {
        return Err(invalid("migration requires 1–32768 UTF-8 bytes"));
    }
    // A conservative first-token gate excludes transaction/session control. The
    // server's extended Parse below enforces exactly one complete statement.
    // Leading comments can be put after the first keyword instead.
    let keyword = sql
        .trim_start()
        .split(|c: char| !c.is_ascii_alphabetic())
        .next()
        .unwrap_or("")
        .to_ascii_uppercase();
    if !matches!(
        keyword.as_str(),
        "CREATE"
            | "ALTER"
            | "DROP"
            | "INSERT"
            | "UPDATE"
            | "DELETE"
            | "COMMENT"
            | "GRANT"
            | "REVOKE"
    ) {
        return Err(invalid(
            "migration must start with CREATE, ALTER, DROP, INSERT, UPDATE, DELETE, COMMENT, GRANT or REVOKE; transaction control and hooks are unsupported",
        ));
    }
    Ok(())
}
#[derive(Default)]
pub struct Workers {
    tasks: BTreeMap<String, JoinHandle<Result<Value>>>,
}
impl Workers {
    pub fn idle(&self) -> bool {
        self.tasks.values().all(JoinHandle::is_finished)
    }
    pub fn finish(&mut self, key: &str) -> Result<Option<Value>> {
        if !self.tasks.get(key).is_some_and(JoinHandle::is_finished) {
            return Ok(None);
        }
        self.tasks
            .remove(key)
            .unwrap()
            .join()
            .map_err(|_| conflict("migration worker interrupted; reconcile its receipt"))?
            .map(Some)
    }
    pub fn contains(&self, key: &str) -> bool {
        self.tasks.contains_key(key)
    }
    pub fn poll(&mut self, key: &str, request: Request) -> Result<Option<Value>> {
        if let Some(task) = self.tasks.get(key) {
            if !task.is_finished() {
                return Ok(None);
            }
            return self
                .tasks
                .remove(key)
                .unwrap()
                .join()
                .map_err(|_| {
                    conflict("migration worker interrupted; reconcile its PostgreSQL receipt")
                })?
                .map(Some);
        }
        // At most four database workers per cell; pending applies remain queued.
        if self.tasks.len() >= 4 {
            return Ok(None);
        }
        let task = std::thread::Builder::new()
            .name("project-migration".into())
            .spawn(move || {
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()?
                    .block_on(request.run())
            })?;
        self.tasks.insert(key.into(), task);
        Ok(None)
    }
}
pub struct Request {
    pub port: u16,
    pub password: String,
    pub origin: String,
    pub deployment: String,
    pub branch: String,
    pub logical: String,
    pub sequence: i64,
    pub sha256: String,
    pub sql: String,
}
struct Connection(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for Connection {
    fn drop(&mut self) {
        self.0.abort();
    }
}
fn failure(e: tokio_postgres::Error) -> crate::store::Error {
    conflict(format!(
        "migration PostgreSQL failure (SQLSTATE {}); current transaction was not acknowledged. Retry the same checksummed migration to reconcile its receipt; earlier migrations remain committed",
        e.code().map_or("connection_unknown", |c| c.code())
    ))
}
impl Request {
    async fn run(self) -> Result<Value> {
        validate(self.sql.as_bytes())?;
        tokio::time::timeout(Duration::from_secs(30), async {
            // A recovered catalog can report a ready revision while its compute
            // is still restarting. Wait for authentication before sending SQL.
            let connecting = std::time::Instant::now();
            let (mut client, conn) = loop {
                let result = async {
                    let socket = tokio::net::TcpStream::connect(("127.0.0.1", self.port)).await?;
                    Config::new().user("cloud_admin").password(&self.password).dbname("postgres")
                        .application_name("supabricks-project-migration")
                        .options("-c statement_timeout=20000 -c lock_timeout=1000 -c idle_in_transaction_session_timeout=5000 -c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1")
                        .connect_raw(crate::query::FramedSocket::new(socket), NoTls).await.map_err(failure)
                }.await;
                match result {
                    Ok(connection) => break connection,
                    Err(error) if connecting.elapsed() >= Duration::from_secs(20) => return Err(error),
                    Err(_) => tokio::time::sleep(Duration::from_millis(100)).await,
                }
            };
            let _connection = Connection(tokio::spawn(conn));
            let tx = client.transaction().await.map_err(failure)?;
            tx.batch_execute("SELECT pg_advisory_xact_lock(1937076322, 5); SET LOCAL ROLE supabricks_owner").await.map_err(failure)?;
            // Share I03's private schema using its existing owner/receipt contract.
            tx.batch_execute(include_str!("../ingest/receipt.sql")).await.map_err(failure)?;
            tx.batch_execute("RESET ROLE; CREATE TABLE IF NOT EXISTS _supabricks.project_migrations (origin text NOT NULL, deployment text NOT NULL, branch text NOT NULL, logical text NOT NULL, sequence bigint NOT NULL, sha256 text NOT NULL, PRIMARY KEY(origin,deployment,branch,logical), UNIQUE(origin,deployment,branch,sequence))").await.map_err(failure)?;
            if let Some(row) = tx.query_opt("SELECT sequence,sha256 FROM _supabricks.project_migrations WHERE origin=$1 AND deployment=$2 AND branch=$3 AND logical=$4", &[&self.origin,&self.deployment,&self.branch,&self.logical]).await.map_err(failure)? {
                if row.get::<_,i64>(0) != self.sequence || row.get::<_,String>(1) != self.sha256 {
                    return Err(conflict("committed migration checksum or sequence changed; add a new migration"));
                }
            } else {
                let last: Option<i64> = tx.query_one("SELECT max(sequence) FROM _supabricks.project_migrations WHERE origin=$1 AND deployment=$2 AND branch=$3", &[&self.origin,&self.deployment,&self.branch]).await.map_err(failure)?.get(0);
                if last.is_some_and(|n| n >= self.sequence) { return Err(conflict("migration sequence precedes an already committed migration")); }
                tx.batch_execute("SET LOCAL ROLE supabricks_owner").await.map_err(failure)?;
                // Parse rejects semicolon-separated commands before any execute.
                let statement = tx.prepare(&self.sql).await.map_err(failure)?;
                tx.execute(&statement, &[]).await.map_err(failure)?;
                tx.batch_execute("RESET ROLE").await.map_err(failure)?;
                tx.execute("INSERT INTO _supabricks.project_migrations VALUES ($1,$2,$3,$4,$5,$6)", &[&self.origin,&self.deployment,&self.branch,&self.logical,&self.sequence,&self.sha256]).await.map_err(failure)?;
            }
            tx.commit().await.map_err(failure)?;
            super::checkpoint("migration_committed");
            Ok(json!({"boundary":"committed","logical":self.logical,"sequence":self.sequence,"sha256":self.sha256}))
        }).await.map_err(|_| conflict("migration deadline exceeded; transaction outcome unknown until its PostgreSQL receipt is reconciled; earlier committed migrations are retained"))?
    }
}
