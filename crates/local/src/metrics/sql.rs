use super::*;
use tokio_postgres::{Config, NoTls};
struct Connection(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for Connection {
    fn drop(&mut self) {
        self.0.abort();
    }
}
fn pg(_: tokio_postgres::Error) -> crate::store::Error {
    conflict("metrics collection failed")
}
pub(super) fn collect(port: u16, password: String) -> Result<Raw> {
    tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(async {
        tokio::time::timeout(Duration::from_secs(4),async {
            let (client,connection)=Config::new().host("127.0.0.1").port(port).dbname("postgres").user("cloud_admin").password(password)
                .application_name("supabricks-metrics")
                .options("-c statement_timeout=1500 -c lock_timeout=250 -c search_path=pg_catalog -c timezone=UTC -c datestyle=ISO -c stats_fetch_consistency=snapshot -c default_transaction_read_only=on -c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1")
                .connect(NoTls).await.map_err(pg)?;
            let _connection=Connection(tokio::spawn(connection));
            // Only built-in aggregates, never query text, relation names or user functions.
            // WAL is compute-wide; database counters/size cover only postgres.
            let row=client.query_one(r#"SELECT d.datid::text,pg_postmaster_start_time()::text,d.stats_reset::text,w.stats_reset::text,
                d.xact_commit,d.xact_rollback,d.blks_hit,d.blks_read,w.wal_bytes::text,
                (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid() AND state='active'),
                (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid() AND state IN ('idle','idle in transaction','idle in transaction (aborted)')),
                (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid() AND (state IS NULL OR state NOT IN ('active','idle','idle in transaction','idle in transaction (aborted)'))),
                pg_database_size(current_database())
                FROM pg_stat_database d CROSS JOIN pg_stat_wal w WHERE d.datname=current_database()"#,&[]).await.map_err(pg)?;
            let counter=|i|->Result<u64> {u64::try_from(row.get::<_,i64>(i)).map_err(|_|conflict("negative PostgreSQL counter"))};
            Ok(Raw {database_oid:row.get(0),postmaster_start:row.get(1),database_reset:row.get(2),wal_reset:row.get(3),commits:counter(4)?,rollbacks:counter(5)?,hits:counter(6)?,reads:counter(7)?,wal_bytes:row.get::<_,String>(8).parse().map_err(|_|conflict("invalid WAL counter"))?,connections_active:counter(9)?,connections_idle:counter(10)?,connections_other:counter(11)?,database_size_bytes:counter(12)?})
        }).await.map_err(|_|conflict("metrics collection deadline exceeded"))?
    })
}
