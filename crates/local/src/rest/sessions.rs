//! Sessions of a branch: who is connected to its PostgreSQL, what each is
//! doing, who waits on whom, and stopping one. Asked as the engine's control
//! role, because the application role sees only its own statements.
use super::{
    Call, Local, Problem, Reply,
    roles::read_as,
    shared::{cursor, page},
    tables::{As, place},
};
use crate::store::Store;
use serde_json::Value;
use std::collections::BTreeMap;
use supabricks_core::resource::{BranchId, DesiredState};

/// A moment as the contract writes one.
macro_rules! at {
    ($column:literal) => {
        concat!(
            "to_char(",
            $column,
            " AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')"
        )
    };
}
/// The server's own sessions belong to superusers and are left out, as is
/// everything that is not a client's connection.
const THEIRS: &str = "a.backend_type = 'client backend' AND NOT r.rolsuper";
/// A session as one JSON object, from `pg_stat_activity a`.
const SESSION: &str = concat!(
    "json_build_object('pid', a.pid, 'role', a.usename, 'application', a.application_name, ",
    "'state', coalesce(a.state, 'unknown'), ",
    "'connected_at', ",
    at!("a.backend_start"),
    ", 'transaction_started_at', ",
    at!("a.xact_start"),
    ", 'query_started_at', ",
    at!("a.query_start"),
    ", 'duration_ms', (extract(epoch FROM clock_timestamp() - CASE WHEN a.state = 'active' ",
    "THEN a.query_start ELSE a.state_change END) * 1000)::bigint, ",
    "'wait_event_type', a.wait_event_type, 'wait_event', a.wait_event, 'query', a.query, ",
    "'blocked_by', to_json(pg_blocking_pids(a.pid)), ",
    "'waiting_for', (SELECT json_build_object('lock', l.locktype, 'mode', l.mode, 'relation', ",
    "(SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ",
    "ON n.oid = c.relnamespace WHERE c.oid = l.relation)) ",
    "FROM pg_locks l WHERE l.pid = a.pid AND NOT l.granted LIMIT 1))"
);

fn no_session() -> Problem {
    Problem::not_found("There is no such session on this branch. It may have ended.")
}
/// Whether the branch has a compute to ask. A branch that is not running has
/// no sessions, and is not woken to be told so.
fn running(store: &Store, branch: BranchId) -> Result<bool, Problem> {
    let record = store.branch(branch).map_err(|e| Problem::from(&e))?;
    Ok(record.endpoint.desired_state == DesiredState::Running)
}

pub fn list(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let limit = call.limit()?;
    let wrong = || Problem::invalid("cursor is not one this server issued.");
    let after = match call.cursor()? {
        None => 0,
        Some(position) => match <[String; 1]>::try_from(position) {
            Ok([pid]) => pid.parse::<i64>().map_err(|_| wrong())?,
            Err(_) => return Err(wrong()),
        },
    };
    if !running(store, at.1)? {
        return Ok(page(Vec::new(), None));
    }
    let sql = format!(
        "SELECT {SESSION}::text FROM pg_stat_activity a JOIN pg_roles r ON r.oid = a.usesysid \
         WHERE {THEIRS} AND a.pid > {after} ORDER BY a.pid LIMIT {}",
        limit + 1
    );
    let rows = match read_as(store, host, call, at, sql, limit + 1, As::Control)? {
        Ok(rows) => rows,
        Err(wait) => return Ok(wait),
    };
    let mut sessions: Vec<Value> = rows
        .iter()
        .filter_map(|row| serde_json::from_str(row.get(0)?.as_str()?).ok())
        .collect();
    let next = (sessions.len() > limit).then(|| {
        sessions.truncate(limit);
        cursor(&[&sessions.last().unwrap()["pid"].to_string()])
    });
    Ok(page(sessions, next))
}

/// Ask PostgreSQL to signal one session, if it is a client's.
fn signal(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    pid: &str,
    function: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let pid: i32 = pid
        .parse()
        .ok()
        .filter(|pid| *pid > 0)
        .ok_or_else(no_session)?;
    if !running(store, at.1)? {
        return Err(no_session());
    }
    let sql = format!(
        "SELECT {function}(a.pid)::text FROM pg_stat_activity a JOIN pg_roles r ON r.oid = a.usesysid \
         WHERE {THEIRS} AND a.pid = {pid}"
    );
    let rows = match read_as(store, host, call, at, sql, 1, As::Control)? {
        Ok(rows) => rows,
        Err(wait) => return Ok(wait),
    };
    // No row: no such client session. False: it ended as it was signalled.
    if rows.first().and_then(|row| row.get(0)?.as_str()) != Some("true") {
        return Err(no_session());
    }
    Ok(Reply {
        status: 204,
        headers: BTreeMap::new(),
        body: None,
        wait: None,
    })
}
pub fn cancel(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    pid: &str,
) -> Result<Reply, Problem> {
    signal(store, host, call, path, pid, "pg_cancel_backend")
}
pub fn terminate(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    pid: &str,
) -> Result<Reply, Problem> {
    signal(store, host, call, path, pid, "pg_terminate_backend")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_moment_is_written_in_utc_as_the_contract_does() {
        assert_eq!(
            at!("a.query_start"),
            "to_char(a.query_start AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')"
        );
        assert!(SESSION.contains("'blocked_by', to_json(pg_blocking_pids(a.pid))"));
        assert!(THEIRS.contains("NOT r.rolsuper"));
    }
}
