# Local application workflow (P06)

Supabricks now exposes the native PG17 cell through a project CLI and stdio MCP.
The native binary is `cargo build --locked -p supabricks-local` →
`target/debug/supabricks`. The first startup still needs the qualified E01 engine
bundle and P03 helpers; see [native cell setup](../architecture/native-cell.md).
The public download/one-command installer belongs to R01. No Kubernetes, model
account or API key is required to run the database or sample app.

## First project

Use an empty project directory (or an existing application's root):

```sh
supabricks init orders
supabricks up --bundle /absolute/engine --helpers /absolute/helpers
supabricks database create main --key initial-main --wait
supabricks branch use main
supabricks connect
supabricks doctor
```

`init` publishes `supabricks.toml` atomically and recovers the same project ID on
retry. Commit that file. It contains only public identity and name. Credentials,
ports, branch selection and durable operations live under the private data root.
The default root is `SUPABRICKS_DATA_DIR`, otherwise `~/.supabricks`. Every command
accepts `--data-dir PATH`. Paths must fit the OS's Unix socket path limit.

CLI project discovery walks up from the current directory; `--project PATH`
selects an exact project root. A second git worktree shares the committed project
ID but has its own selection. Run `branch use NAME` there. An unselected worktree
fails explicitly; it never falls back to another worktree or a project default.
Changing the default protects a resource from deletion; it does not select it.

A database is an independent root timeline with an application database named
`postgres`; branches fork it. `database list` and `branch list` show the same
project inventory, with parent identity and `is_default` distinguishing roots
and the protected default. Names and UUIDs identify resources. Database roots
use the same lifecycle commands as branches.

## Branch before a migration

```sh
supabricks branch create add-status --from main --key add-status --wait
supabricks branch use add-status
supabricks sql --branch add-status --write --file migrations/002-status.sql
supabricks catalog --branch add-status
supabricks catalog --branch main
supabricks branch use main
supabricks branch delete add-status --wait
```

See the runnable [orders application](../../examples/orders/README.md), including
its initial schema, parameterized HTTP writes and migration. Branch creation also
accepts `--at-lsn LSN` or `--at-time RFC3339`; retained-history rules from P04 apply.

`connect [NAME]` returns a stable application URI and credentials. `--uri` prints
only the URI for applications. Keep this output private; do not commit it or paste
it into tickets. Connecting wakes a suspended branch. `branch suspend NAME`
refuses active connections, including idle pools. `down` stops the whole cell,
retains data, and closes clients. `up` recovers desired running branches. An app
must reconnect after `down`; its URI remains valid. There is no automatic idle
suspension yet.

Deletion is always `branch delete NAME`. The protected default needs a replacement
via `branch default NAME`, or explicit `--force`. Force disconnects clients and
removes default protection. Children still block deletion. `branch ttl NAME
--expires-at-ms TIMESTAMP` sets a future Unix millisecond expiration; `none`
clears it. Default branches cannot expire.

## Machine contract

JSON is the default (`--json` is accepted). Successful commands emit one JSON
object to stdout; errors emit `{"api_version":1,"error":...}` to stderr. Errors
include code, message, hint, retryability and exit code. `connect --uri`, help,
version and MCP stdio are the explicit non-JSON-command-output forms.

| Exit | Meaning |
|---|---|
| 0 | Success, or a durable operation was accepted |
| 1 | Local filesystem/I/O failure |
| 2 | Invalid command, project or request |
| 3 | Missing project-scoped resource or worktree selection |
| 4 | Lifecycle, revision, ownership or idempotency conflict |
| 5 | Daemon/engine unavailable or SQL worker capacity exhausted |
| 6 | PostgreSQL error, SQL deadline or result limit |
| 7 | Observed operation failed or was superseded |
| 8 | Waiting deadline reached; operation continues |

Lifecycle commands return `id`, `branch_id`, `key`, `revision`, `status`, `steps`,
`next_step`, `results` and `error` immediately. Acceptance is not completion.
`operation get ID` observes progress. `operation wait ID --timeout-ms 90000` polls;
changed progress goes to stderr and the final result goes to stdout. `--wait` on
a mutation combines these steps, with the accepted ID on stderr. Runtime `up` and
`down` have a bounded readiness/cleanup wait with progress on stderr; `status` and
`doctor` expose installation progress. They are installation commands, not branch
journal operations.

CLI mutations generate an idempotency key unless `--key KEY` is supplied. Reuse
that key only with identical inputs; changed inputs conflict. Lifecycle commands
accept `--revision N`; otherwise the CLI reads identity and revision before
submitting. On a retry, use the returned branch UUID, original key and revision,
or just inspect the returned operation ID. Never blindly retry a SQL write after
a transport error: PostgreSQL may have committed before the reply was lost.

`doctor` reports runtime availability and private log location, and optionally
validates `--project`. Operation-specific errors appear in `operation get`.
Daemon and PostgreSQL logs are private and can include SQL data.

## SQL and local socket API

`sql --sql SQL` or `--file PATH` executes exactly one statement. PostgreSQL's
extended Parse validates that boundary; a streaming simple query of the identical
statement returns lossless PostgreSQL text values (or null), with column names,
types and OIDs. This avoids rounding numeric/bigint values in JSON. SQL parameters,
COPY streaming and persistent transactions belong in an ordinary PostgreSQL
client; the sample uses psycopg with bound parameters.

SQL starts in a read-only transaction. Writes require `--write --branch NAME`
(or MCP `read_only=false` with an explicit branch). Work runs as `supabricks_owner`
through the stable gateway. Four dedicated workers keep the daemon's lifecycle
loop responsive. Each has a 32-second connection deadline, a configurable
100–30000 ms SQL deadline (default 10000), a 45-second total budget including
connection and cancellation, statement and lock timeouts, up to
1000 rows (default 200), 32 KiB SQL, a 256 KiB result budget and a 1 MiB backend
frame ceiling enforced before body allocation. Limit failures cancel and close
the session; incomplete results are errors. Writes commit only after result
collection. A connection loss at commit has an uncertain outcome. Catalog
uses the same read-only path and limits; for larger catalogs use targeted SQL.

The application API is `supabricks.local` version 1 inside the existing private
socket envelope:

```json
{"version":1,"request":{"method":"api","api_version":1,"binding":{"project_id":"PROJECT_UUID","worktree":"/absolute/worktree"},"action":{"action":"list_branches"}}}
```

Every application request revalidates the worktree's project identity. Operation
lookups, branches and selections are project-scoped. Lifecycle mutations share the
existing SQLite journal, revision fences, leases, pins and gateway rules. Backend
ports are allocated while OS-bound and durably reserved; retries recover the
original allocation. Public creation admits at most 32 active branches per cell
(including suspended branches and incomplete deletion). An unrelated process
stealing a released startup port causes
a startup conflict rather than retargeting a database.

Requests are newline JSON, at most 64 KiB. Responses use the existing
`{"version":1,"result":...}` or `error` envelope; shared clients cap responses at
2 MiB. The old unscoped socket methods remain private engineering/qualification
interfaces. The filesystem owner can use them or read state directly: project
binding prevents accidental cross-project actions, and is not a multi-user
security boundary. The old operator HTTP/MCP API remains separate and unchanged.

## Agents and qualification

The [agent adapter and manual setup](../../agents/README.md) launch
`supabricks mcp --project PATH --data-dir PATH`. The process has a fixed binding;
it checks it again for every request and reconnects to the daemon after restart.
There are 25 discoverable tools, including capabilities, catalog, SQL, selection,
connections, metadata operations and operation polling. The MCP protocol revision
is 2025-06-18. Tools return matching text and structured JSON; tool failures use
`isError`, while malformed protocol/arguments use JSON-RPC errors. No tool shuts
down the installation. Lifecycle protections are enforced by the daemon.

Portable tests cover the public socket and CLI contracts, immutable MCP binding,
version negotiation, strict arguments and a separate local tool schema snapshot.
`e2e/native/workflow.py` qualifies the CLI, generic MCP session, orders HTTP app,
branch isolation, SQL limits, worker admission, suspension and restart on Linux
and macOS. P03–P05 native suites remain gates. The installed-agent usability check
is recorded separately in [P06 qualification](../architecture/p06-qualification.md).
