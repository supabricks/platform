use super::*;
use tokio_postgres::{Client, Config, NoTls};
pub(super) struct Input {
    pub port: u16,
    pub password: String,
    pub include_query_text: bool,
    pub observation: Option<Value>,
    pub terminate: bool,
}
struct Connection(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for Connection {
    fn drop(&mut self) {
        self.0.abort();
    }
}
fn pg(error: tokio_postgres::Error) -> crate::store::Error {
    conflict(format!(
        "activity SQL failed ({}); refresh before retrying a signal",
        error
            .code()
            .map(|c| c.code())
            .unwrap_or("connection outcome uncertain")
    ))
}
pub(super) fn run(input: Input) -> Result<Value> {
    tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(async {
        tokio::time::timeout(Duration::from_secs(4),async {
            let (client,connection)=Config::new().host("127.0.0.1").port(input.port).dbname("postgres")
                .user("cloud_admin").password(&input.password).application_name("supabricks-activity")
                .options("-c statement_timeout=1500 -c lock_timeout=250 -c search_path=pg_catalog -c timezone=UTC -c datestyle=ISO -c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1")
                .connect(NoTls).await.map_err(pg)?;
            let _connection=Connection(tokio::spawn(connection));
            if let Some(observation)=input.observation {signal(&client,observation,input.terminate).await}
            else {list(&client,input.include_query_text).await}
        }).await.map_err(|_|conflict("activity deadline exceeded; signal outcome may be uncertain, refresh before acting again"))?
    })
}
async fn list(client: &Client, include_text: bool) -> Result<Value> {
    // Internal/control roles never expose query text or signal eligibility.
    // This operator slice only controls the platform's local application role.
    let rows=client.query(r#"
        SELECT a.pid, left(a.usename,128), left(a.state,64), left(a.wait_event_type,64), left(a.wait_event,128),
          a.backend_start::text, a.query_start::text, a.xact_start::text,
          CASE WHEN a.state='active' THEN greatest(0,extract(epoch FROM clock_timestamp()-a.query_start)*1000)::float8 END,
          greatest(0,extract(epoch FROM clock_timestamp()-a.state_change)*1000)::float8,
          left(a.backend_type,64),
          CASE WHEN $1 AND a.usename='supabricks_owner' AND a.backend_type='client backend' AND NOT coalesce(r.rolsuper,true) THEN left(a.query,1024) END,
          a.usename='supabricks_owner' AND a.backend_type='client backend' AND NOT coalesce(r.rolsuper,true),
          ARRAY(SELECT p FROM unnest(pg_blocking_pids(a.pid)) p LIMIT 65),
          pg_postmaster_start_time()::text
        FROM pg_stat_activity a LEFT JOIN pg_roles r ON r.oid=a.usesysid
        WHERE a.datname=current_database() AND a.pid<>pg_backend_pid()
        ORDER BY (a.state='active') DESC NULLS LAST,a.pid LIMIT 201
    "#,&[&include_text]).await.map_err(pg)?;
    let truncated = rows.len() > 200;
    let mut sessions = Vec::new();
    let mut pids = Vec::new();
    for row in rows.into_iter().take(200) {
        let pid: i32 = row.get(0);
        pids.push(pid);
        let state: Option<String> = row.get(2);
        let eligible: bool = row.get(12);
        let mut blockers: Vec<i32> = row.get(13);
        let blockers_truncated = blockers.len() > 64;
        blockers.truncate(64);
        let text: Option<String> = row.get(11);
        sessions.push(json!({"pid":pid,"role":row.get::<_,Option<String>>(1),"state":state,"wait_event_type":row.get::<_,Option<String>>(3),"wait_event":row.get::<_,Option<String>>(4),
          "backend_start":row.get::<_,Option<String>>(5),"query_start":row.get::<_,Option<String>>(6),"transaction_start":row.get::<_,Option<String>>(7),
          "query_duration_ms":row.get::<_,Option<f64>>(8),"state_duration_ms":row.get::<_,Option<f64>>(9),"backend_type":row.get::<_,Option<String>>(10),
          "query_text":text.map(bound_text),"query_text_policy":if include_text && eligible {"operator_requested_raw_bounded"}else{"hidden"},
          "can_cancel":eligible && state.as_deref()==Some("active"),"can_terminate":eligible,"blocking_pids":blockers,"blockers_truncated":blockers_truncated}));
    }
    // Waited-on lock objects only. PostgreSQL's blocker function above is the
    // authority for edges; a naive pg_locks self-join misses queue ordering.
    let locks=client.query(r#"SELECT l.pid,l.locktype,l.mode,l.database::text,l.relation::text,
        left(n.nspname,128),left(c.relname,128),l.page,l.tuple::int4,l.transactionid::text,l.virtualxid,
        l.classid::text,l.objid::text,l.objsubid::int4
        FROM pg_locks l LEFT JOIN pg_class c ON c.oid=l.relation AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
        LEFT JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE NOT l.granted AND l.pid=ANY($1) ORDER BY l.pid,l.locktype,l.mode LIMIT 501"#,&[&pids]).await.map_err(pg)?;
    let locks_truncated = locks.len() > 500;
    let locks:Vec<_>=locks.into_iter().take(500).map(|r|json!({"pid":r.get::<_,Option<i32>>(0),"type":r.get::<_,String>(1),"mode":r.get::<_,String>(2),"database_oid":r.get::<_,Option<String>>(3),"relation_oid":r.get::<_,Option<String>>(4),"schema":r.get::<_,Option<String>>(5),"relation":r.get::<_,Option<String>>(6),"page":r.get::<_,Option<i32>>(7),"tuple":r.get::<_,Option<i32>>(8),"transaction_id":r.get::<_,Option<String>>(9),"virtual_xid":r.get::<_,Option<String>>(10),"class_id":r.get::<_,Option<String>>(11),"object_id":r.get::<_,Option<String>>(12),"object_sub_id":r.get::<_,Option<i32>>(13)})).collect();
    let start: String = client
        .query_one("SELECT pg_postmaster_start_time()::text", &[])
        .await
        .map_err(pg)?
        .get(0);
    let result = json!({"sessions":sessions,"waiting_locks":locks,"sessions_truncated":truncated,"locks_truncated":locks_truncated,"postmaster_start":start,"observed_at_ms":crate::identity::now(),"consistent_snapshot":false,"query_text_persisted":false,"governed_supported":false});
    if serde_json::to_vec(&result)?.len() > 512 * 1024 {
        return Err(conflict("activity response exceeds 512 KiB"));
    }
    Ok(result)
}
async fn signal(client: &Client, observation: Value, terminate: bool) -> Result<Value> {
    let pid = observation["pid"]
        .as_i64()
        .ok_or_else(|| invalid("missing session PID"))? as i32;
    let birth = observation["backend_start"]
        .as_str()
        .ok_or_else(|| invalid("missing backend identity"))?;
    let query = observation["query_start"].as_str();
    let start = observation["postmaster_start"]
        .as_str()
        .ok_or_else(|| invalid("missing compute identity"))?;
    // Revalidate in the same SQL statement as the signal. PostgreSQL's signal
    // API is best effort: completion can race this observation; never retry it.
    let function = if terminate {
        "pg_terminate_backend(a.pid,0)"
    } else {
        "pg_cancel_backend(a.pid)"
    };
    let sql = format!(
        r#"SELECT {function} FROM pg_stat_activity a JOIN pg_roles r ON r.oid=a.usesysid
      WHERE a.pid=$1 AND a.datname=current_database() AND a.usename='supabricks_owner'
      AND NOT r.rolsuper AND a.backend_type='client backend' AND a.pid<>pg_backend_pid()
      AND a.backend_start::text=$2 AND a.query_start::text IS NOT DISTINCT FROM $3
      AND pg_postmaster_start_time()::text=$4 AND ($5 OR a.state='active')"#
    );
    let row = client
        .query_opt(&sql, &[&pid, &birth, &query, &start, &terminate])
        .await
        .map_err(pg)?
        .ok_or_else(|| conflict("session or query changed since snapshot; refresh activity"))?;
    let sent: bool = row.get(0);
    Ok(
        json!({"pid":pid,"action":if terminate {"terminate"}else{"cancel"},"signal_sent":sent,"completion_confirmed":false,"refresh_required":true}),
    )
}
fn bound_text(text: String) -> String {
    let mut output = String::new();
    for c in text.chars() {
        if output.len() + c.len_utf8() > 1024 {
            break;
        }
        if !c.is_control() || matches!(c, '\n' | '\t') {
            output.push(c)
        }
    }
    output
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn displayed_text_has_a_byte_bound_and_no_terminal_controls() {
        let result = bound_text(format!("\x1b\0{}", "界".repeat(1000)));
        assert!(result.len() <= 1024);
        assert!(!result.chars().any(char::is_control));
        assert_eq!(bound_text("select\n\t1".into()), "select\n\t1");
    }
}
