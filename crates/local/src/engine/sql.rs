//! Bounded internal SQL; application credentials never authorize control work.
use crate::store::{Result, error::conflict};
use std::time::Duration;
use supabricks_core::{keys::pg_md5, lsn::Lsn};
use tokio_postgres::{Config, NoTls};

pub struct Sql(tokio::runtime::Runtime);
enum Task<'a> {
    Flush,
    Provision(&'a str, bool),
    Drain,
}
struct ConnectionTask(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for ConnectionTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}
impl Sql {
    pub fn new() -> Result<Self> {
        Ok(Self(
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?,
        ))
    }
    fn run(&self, port: u16, password: &str, task: Task<'_>) -> Result<Option<String>> {
        self.0.block_on(async {
            tokio::time::timeout(Duration::from_secs(4), async {
                let (client, connection) = Config::new()
                    .host("127.0.0.1")
                    .port(port)
                    .user("cloud_admin")
                    .password(password)
                    .dbname("postgres")
                    .application_name("supabricks-control")
                    .options("-c statement_timeout=2000 -c lock_timeout=1000 -c search_path=pg_catalog -c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1")
                    .connect(NoTls)
                    .await
                    .map_err(|_| conflict("internal SQL connection unavailable"))?;
                let _connection = ConnectionTask(tokio::spawn(connection));
                tokio::time::timeout(Duration::from_secs(3), execute(&client, task))
                    .await
                    .map_err(|_| conflict("internal SQL deadline exceeded"))?
                    .map_err(|_| conflict("internal SQL control request failed"))
            })
            .await
            .map_err(|_| conflict("internal SQL deadline exceeded"))?
        })
    }
    pub fn flush(&self, port: u16, password: &str) -> Result<Lsn> {
        self.run(port, password, Task::Flush)?
            .and_then(|s| s.parse().ok())
            .ok_or_else(|| conflict("invalid PostgreSQL flush boundary"))
    }
    pub fn provision(
        &self,
        port: u16,
        password: &str,
        app_password: &str,
        expired: bool,
    ) -> Result<()> {
        self.run(port, password, Task::Provision(app_password, expired))
            .map(|_| ())
    }
    pub fn drain(&self, port: u16, password: &str) -> Result<bool> {
        Ok(self.run(port, password, Task::Drain)?.as_deref() == Some("0"))
    }
}
async fn execute(
    client: &tokio_postgres::Client,
    task: Task<'_>,
) -> std::result::Result<Option<String>, tokio_postgres::Error> {
    match task {
        Task::Provision(password, expired) => {
            client.batch_execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='supabricks_owner') THEN CREATE ROLE supabricks_owner NOLOGIN; END IF; END $$").await?;
            // Only fixed keywords and a hex digest are formatted.
            let hash = pg_md5(password, "supabricks_owner");
            let login = if expired { "NOLOGIN" } else { "LOGIN" };
            client.batch_execute(&format!("ALTER ROLE supabricks_owner {login} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'md5{hash}'")).await?;
            client
                .batch_execute("ALTER DATABASE postgres OWNER TO supabricks_owner")
                .await?;
            Ok(None)
        }
        Task::Flush => Ok(Some(
            client
                .query_one("SELECT pg_current_wal_flush_lsn()::text", &[])
                .await?
                .get(0),
        )),
        Task::Drain => {
            client
                .batch_execute("ALTER ROLE supabricks_owner NOLOGIN")
                .await?;
            Ok(Some(client.query_one("SELECT count(*)::text FROM pg_stat_activity WHERE usename='supabricks_owner'", &[]).await?.get(0)))
        }
    }
}
