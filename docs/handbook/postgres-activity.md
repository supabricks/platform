# Live PostgreSQL activity

The local operator console workspace API, CLI and MCP expose running sessions,
wait events, blocking PIDs and waited-on lock objects for an explicit branch.
This is the live-activity slice of [#248](https://github.com/supabricks/platform/issues/248).
It works with the existing PG17 engine; `pg_stat_statements` is not required.
Statement rankings and historical metrics belong to subsequent slices.

```sh
supabricks activity list --branch main
supabricks activity list --branch main --include-query-text
supabricks activity cancel --branch main --snapshot SNAPSHOT_UUID --pid 12345
supabricks activity terminate --branch main --snapshot SNAPSHOT_UUID --pid 12345
```

Use `--project` and `--data-dir` as usual. The list response's `id` is the snapshot
UUID. Only act on rows whose `can_cancel` or `can_terminate` is true. The CLI
waits for the bounded background request and prints its receipt.

## API and console contract

The local API v1 action, console workspace command and MCP `activity` tool use
`{"action":"activity","command":COMMAND}`. MCP arguments contain the inner
`command` field. Commands are specified by the
[JSON schema](../../schemas/activity-command-v1.schema.json):

```json
{"action":"list","target":{"branch":"BRANCH_UUID","revision":1},"include_query_text":false}
```

List returns `{id, state:"running", target, generation, expires_after_seconds}`.
Poll with `{"action":"poll","id":"REQUEST_UUID"}` until `state` is `succeeded`
or `failed`. Success includes `result.sessions`, `result.waiting_locks`, sample
time and truncation flags. Failure includes a bounded diagnostic without SQL
text or private connection details. Session durations are milliseconds; query
duration is null for idle sessions, while state duration remains available.
Blocking PIDs come from PostgreSQL's `pg_blocking_pids`; a zero PID can represent
a prepared transaction. Locks include type, mode and available relation or
transaction/advisory identifiers. Live views may change between reads; the
response explicitly sets `consistent_snapshot:false`. A blocker can be outside
the returned session page.

For a signal, submit a new background request:

```json
{"action":"cancel","target":{"branch":"BRANCH_UUID","revision":1},"snapshot":"LIST_REQUEST_UUID","pid":12345}
```

Use `terminate` to disconnect the session. Repeating the same signal against the
same snapshot/PID returns its existing receipt. A different signal against that
snapshot/PID requires a fresh list. Expired receipts never trigger a replay.

## Authority, identity and query text

Every request validates project ownership and branch revision. This slice uses
local operator authority and explicitly refuses governed branches, including
polling an old receipt after governance changes. Separate governed monitoring
and session-control permissions remain part of the outstanding #248 work.
Only the local `supabricks_owner` client sessions are eligible for signals.
Administrator, replication and other internal sessions are protected; other
roles are shown as protected metadata in this initial contract.

Signals require a retained snapshot and recheck the PostgreSQL postmaster start,
backend start, query start, role, backend type and database. Snapshot identities
expire on daemon restart. These checks reject already-stale targets, but
PostgreSQL's signal API is best effort: query completion can still race the
check and delivery. A successful receipt means `signal_sent`, not completed
cancellation/termination; refresh activity to observe the outcome. No automatic
signal retry occurs after timeout or connection loss. A write interrupted near
COMMIT may already have committed.

Query text is hidden by default. `include_query_text:true` explicitly requests
raw SQL for eligible local application sessions, limited to 1,024 UTF-8 bytes
with terminal control characters removed. **This is bounded text, not guaranteed
redaction:** literals, comments and identifiers may contain application data.
PostgreSQL may already have truncated its activity text. Internal query text is
always hidden, and idle session text, when requested, is the previous statement.
The API keeps text only in short-lived memory; it writes no query history,
metrics, audit SQL text or external telemetry. This does not change PostgreSQL's
own configured logging behavior.

## Resource and lifecycle bounds

Two dedicated workers execute activity SQL; the daemon's control loop does not
wait on it. Requests have a four-second overall deadline, 1.5-second statement
deadline and 250-ms lock deadline. Shutdown drains admitted workers before
stopping the cell. The service retains at most 16 jobs for up to 30 seconds;
capacity pressure can evict older completed receipts sooner. Each result is
limited to 200 sessions, 64 blockers per session, 500 waiting locks and 512 KiB.
No metrics database or new external service is introduced.

A suspended or unreconciled compute is refused without waking it. An already
completed snapshot can still be stale even inside its lifetime; refresh before
acting and honor revision conflicts. Polls and signals from another project
cannot access its receipts.

## Focused qualification

The [retained fixture receipt](../architecture/pg248a-evidence/activity.json)
records 12 checks against the existing engine: real lock contention, text access,
query cancel versus connection terminate, signal retry receipts, stale queries,
protected administrator sessions, project and revision boundaries, CLI behavior,
suspension and restart. Four focused Rust tests cover command validation, text
bounds, eligible observations, receipt expiry and authority. The MCP contract
snapshot is also checked. The fixture runs in native-cell GitHub CI on both
targets; a local Linux pass does not assert macOS qualification.
