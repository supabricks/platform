# C02: PostgreSQL database workspace

C02 extends the [C01 console](c01-console.md) with explicit PostgreSQL operations:
branch lifecycle, catalog/preview, asynchronous query handles, targeted cancel,
connection reveal and saved queries. The alpha.5 archives retain catalog version
8 and the existing PG17/analytical inventories. No engine or public CLI/MCP
contract change is required. The [runbook](../handbook/database-workspace.md)
describes user behavior and limits.

## Dispatch and ownership

The bridge adds one typed `/api/workspace` POST endpoint. It requires the existing
exact Host/Origin, version header, authenticated HttpOnly session and CSRF secret.
A 60,000-byte body cap leaves envelope room below the private socket's 64 KiB
limit. Unknown commands/fields are rejected. The endpoint accepts an explicit
command enum rather than forwarding arbitrary public API methods. It exposes no
file path, shell, runtime configuration, forced deletion or worktree-selection
operation.

`ConsoleAction` carries the trusted bridge's project/worktree binding, daemon
generation and instance/session identity. The daemon revalidates the binding and
generation on every request. Database work carries a UUID/revision pair; the sole
writer checks project ownership, revision and accepting-work state before issuing
an effect. Branch actions reuse existing idempotency keys and durable operations.
Browser operation responses select progress/error fields and omit internal effect
results. Connection credentials are returned only by the explicit connect command.

Queries reuse the existing PostgreSQL worker implementation and gateway, including
connection accounting, statement/result/frame bounds and lossless text values.
The daemon owns dedicated query threads; there is no second SQLite writer or
new external process. Console and synchronous CLI/MCP SQL share the same four
worker slots. Starting a console query immediately returns a handle, so neither
HTTP requests nor lifecycle reconciliation wait for SQL completion.

## Query lifecycle and failure semantics

A handle records a random query UUID, owner session/worktree, branch revision,
daemon generation, query options, monotonic timing, cancellation channel and
worker completion receiver. Status is running/cancelling followed by succeeded,
failed or cancelled. A duplicate ID with identical retained inputs returns its
existing handle; changed inputs conflict. Neither client nor server retries SQL.
The browser generates a new ID only for an explicit execution. Query IDs are
transient handles, not durable idempotency receipts.

Cancellation signals exactly that worker, including during connection setup.
Once connected, it keeps the cancel socket open through gateway routing until
PostgreSQL closes it (or a one-second bound expires), using tokio-postgres's
backend cancellation token, and closes
the owned connection task on failure/cancellation. A completed result wins a
cancellation race; a request interrupted near COMMIT reports the possible write
ambiguity instead of promising rollback. Daemon shutdown cancels outstanding
console work and joins its threads before releasing Store ownership. Daemon
replacement destroys handles and browser sessions; no SQL is recovered or replayed.

At most 32 recent handles/results are kept in daemon memory, bounded by existing
query/result caps. Terminal handles expire after ten minutes; the oldest terminal
handle can be evicted to admit new work. Active work is never evicted. No SQL,
results or credentials enter automatic browser storage or persistent query logs.
A status lookup outside the owning session/generation returns no query data.

## Saved queries and frontend

The daemon owns version-1 JSON files under `queries/PROJECT_UUID/QUERY_UUID.json`.
Paths derive only from typed IDs. Directories/files are private, owned and regular;
symlinks and incompatible formats are refused. SQL/title/file size and a
100-per-project inventory cap bound reads. Atomic replacement, file/directory
fsync and optimistic saved revisions protect explicit saves. Stopped backup/restore
already inventories this durable directory. Ephemeral authentication remains under
`tmp/` and excluded. This is an additive file format; no SQLite migration is needed.

React workspace views reuse the locked C01 dependencies. The native textarea
supports keyboard execution; a bounded virtual table renders PostgreSQL text/null
without numeric coercion. Eight tabs retain independent branch bindings, SQL and
results in memory while navigation changes. Rebinding and saved-query loading
reset write mode; stale revisions/deleted branches block new work. Catalog and
preview requests target the navigation branch explicitly. A read-only preview is
backend-generated with quoted PostgreSQL identifiers and a 200-row LIMIT.

## Qualification

Portable tests use real daemon/bridge processes for CSRF/allowlist checks,
revision conflicts, private saved-file modes, traversal/symlink rejection and
actual stopped backup/restore. The installed browser harness extends C01 with
real PostgreSQL writes and parent/child isolation, exact numeric/null rendering,
catalog/preview, row/byte/SQL errors, keyboard execution, virtual scrolling,
concurrent targeted cancellation, shared worker capacity, cross-session handle
isolation, lifecycle revision changes/deletion, saved-query reload/restart and
credential reveal. HTTP failures never trigger SQL replay.

Native release jobs build and install the exact alpha.5 archive on Linux x86_64
and macOS arm64, then run Chromium with external networking disabled. JSON reports
identify successful checks and release identity; screenshots use synthetic data.
PR evidence records actual results. Existing recovery, full-snapshot, native and
Kubernetes gates remain in force. Safari/Firefox, power-loss durability, file
import and analytical workspace behavior remain outside C02 qualification.
