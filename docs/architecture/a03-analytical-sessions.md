# A03: pinned Sail sessions and analytical queries

Supabricks opens a separate Sail worker/catalog for each analytical session.
Every source view is bound to one published A02 epoch before the Spark Connect
endpoint becomes available. Refreshing publishes a new epoch for fresh sessions;
existing sessions and their running queries keep their original table versions.

## Developer workflow

Keep the [A01 locked worker environment](a01-frozen-exports.md#developer-setup)
configured. `session.py` and `shell.py` live beside the configured `export.py`.
No extra dependency installation happens on session startup. Packaging these
files and the private Python environment is implemented by [R02](r02-analytical-preview.md).

```sh
# First access automatically exports and publishes if no snapshot exists.
supabricks analytics sql --branch main --sql \
  'SELECT count(*), sum(amount) FROM public.orders'

# Explicit refresh returns an ID; --wait reports progress until publication.
supabricks analytics refresh --branch main --key daily-refresh --wait
supabricks analytics status REFRESH_ID
supabricks analytics cancel-refresh REFRESH_ID

# Reuse a session, or select a retained historical epoch explicitly.
supabricks analytics open --branch main --ttl-ms 900000 --wait
supabricks analytics open --branch main --epoch EPOCH_ID --wait
supabricks analytics session SESSION_ID
supabricks analytics sql --session SESSION_ID --sql \
  'SELECT o.id, p.amount FROM public.orders o JOIN public.payments p ON o.id = p.id'
supabricks analytics close SESSION_ID
supabricks analytics cancel-session SESSION_ID

# Interactive Python, with ordinary spark and epoch variables.
supabricks spark shell --branch main
# Also supports --file analysis.py for a Python script.
```

Session admission and explicit refresh use project-scoped idempotency keys.
Reusing an admission key returns the same session, including after it closes;
use a new key for a fresh worker. An implicit first refresh is shared by waiting
sessions on that branch. Closing a waiting session does not cancel a refresh
that other callers may use. Cancel that refresh explicitly if desired.

`analytics status` reports the complete refresh lifecycle, including the nested
A01 export and A02 publication. Existing unpublished `analytics export` calls
can still be inspected through that command or the unchanged `get_export` API;
they do not publish automatically. `analytics publish` remains available.

The CLI's SQL command opens an ephemeral session when `--session` is omitted
and closes it afterward. SQL queries on an explicit session keep it open.
Shell exit closes its session, including normal script exceptions and Ctrl-C.
A killed client leaves a bounded session that can be closed by ID or expire.

## Ordinary PySpark and epoch metadata

A ready session includes a loopback Spark Connect `endpoint`. Use the **entire**
URI, including its `user_id` and `session_id` parameters, to reconnect to the
catalog Supabricks initialized. Dropping those parameters creates a different
Spark session without those bindings. Run this in a Python script or notebook
using the locked environment:

```python
from pyspark.sql import SparkSession

spark = SparkSession.builder.remote(endpoint).getOrCreate()
spark.table("public.orders").groupBy("id").sum("amount").show()
spark.sql("SELECT * FROM _supabricks.epoch").show(truncate=False)
```

`_supabricks.epoch` is a one-row metadata view with installation, project,
branch, epoch, ordinal, observation/publication times, session lifetime and
`metadata_json` containing the exact source identity/LSN. The Spark configuration
`supabricks.epoch_id` provides a quick lookup. Session status reports
`snapshot_age_ms`, calculated from the export observation time; it is null when
an older manifest lacks that time. Spark DataFrame results remain ordinary
DataFrames and Rows, without a custom result envelope.

The bootstrap creates private named Delta tables in `_supabricks_source`, then
views under the original PostgreSQL schema/table names using explicit
`VERSION AS OF`. Unqualified names use `public`. Source schemas named
`_supabricks` or `_supabricks_source` are rejected to preserve metadata discovery.
Names that collide under Spark case-insensitive resolution are also rejected.
The worker and bundled shell use UTC for timestamp interpretation. External
PySpark clients retain their normal client-side datetime/timezone behavior.
Neither the ignored `versionAsOf` table option nor direct-path persisted views
are used. The named-table mechanism is the one qualified in A00.

The pinned PySpark client treats `python -c` as a doctest context and may try to
find a full Spark distribution. Use a script, notebook or `spark shell`; no JVM
or full Spark distribution is required for these qualified entry points.

## Bounds, cancellation and trust

| Resource | Initial bound |
| --- | --- |
| Active sessions | 2 per installation, including waiting/cleanup |
| Session lifetime | 15 minutes default; 10 seconds–1 hour configurable |
| Worker bootstrap | 120 seconds after launch |
| Managed SQL | One active request/session; 32 KiB SQL; one read query |
| Managed result | 200 rows default, at most 1000; 256 KiB default/max |
| Managed query deadline | 10 seconds default; 100 ms–30 seconds configurable |
| Sail query memory pool | 256 MiB fair pool per worker |
| Sail spill | 256 MiB total per worker; individual files at most 16 MiB |
| Worker RSS | 1 GiB sampled watchdog, checked every 100 ms |

Managed SQL accepts a conservative subset beginning with SELECT, WITH or
EXPLAIN. Mutating statements, multiple statements and script transforms are
rejected before Sail executes them. SQL comments and quoted identifiers/literals
are handled explicitly. Unsupported queries fail without replacing the session.
Bounded results carry column types, text values or null, and a `truncated` flag;
exact decimals never pass through floats during result encoding. Only the latest
managed query/result is retained per session. Starting another query replaces it.

CLI/MCP SQL uses asynchronous `analytics_sql` admission and `analytics_query`
polling internally. A deadline or `analytics_cancel` stops the entire session,
including its raw Spark clients. Reopen a session afterward. There is no silent
retry, worker replacement or rebinding of an existing endpoint.

`spark.stop()` closes its Spark client session; use `analytics close` to release
the Supabricks worker and its durable reference immediately.

Raw Spark Connect/Python clients are trusted execution by the installation's OS
user. They share the worker's lifetime, memory and spill bounds, but their own
DataFrame result collection is not subject to the CLI/MCP row/byte limits.
Supabricks' cancellation operation closes their entire worker. Raw Python/UDFs,
filesystem access and direct engine write APIs are not a security sandbox and
are not supported ways to mutate exported generations. The managed SQL surface
is read-only; separately owned writable lakehouse tables remain a later slice.
Loopback endpoints have no multi-user authentication boundary.

The [Sail memory and temporary-file settings](https://docs.lakesail.com/sail/latest/reference/configuration/)
limit engine-accounted allocations. The RSS watchdog is sampled, not a hard OS
memory quota; allocations can briefly exceed its threshold. Session deadlines
are also enforced by the daemon. The implementation does not claim cgroup-like
isolation on laptops.

## Decimal statistics compatibility

The A03 native test exposed a limitation in delta-rs 1.6.3: its default Delta
min/max statistics can round exact decimal values through floating point.
For a single-row `numeric(20,4)` table containing `1234567890123456.1234`, the
Parquet row was exact but its log statistics contained `1234567890123456.0`.
Sail 0.7.1 could substitute that statistic as the row value and incorrectly
eliminate an equality match. The earlier A00 multi-row decimal fixture did not
expose this single-value statistics optimization.

New A01 exports set `delta.dataSkippingNumIndexedCols=0`, omitting column
statistics while retaining row counts. Sail reads exact Parquet values; this
trades statistics-based pruning for correctness. Session bootstrap rejects an
older epoch containing decimal min/max statistics and asks for an explicit
refresh. Published generations remain immutable. Independent Delta readers and
A02 manifests continue to describe the original files and selected versions.

## Ownership and recovery

Session admission records its epoch reference in SQLite before authorizing any
worker. The existing gated-launch mechanism records PID, birth identity,
process-group ownership and a random token before the child executes Python.
Analytical processes are separate from the Postgres/Process Compose lifecycle;
Postgres suspension and storage-process recovery do not discard their catalogs.

A session is a durable GC reference while waiting, starting, ready or closing.
Expiry alone cannot free its files: cleanup first stops and verifies the entire
owned group, removes private scratch data, then marks the session terminal.
GC also protects a published refresh awaiting binding by a waiting session.
A crash during cleanup leaves that reference intact until recovery finishes.

Daemon restart stops surviving analytical workers using their recorded ownership
and marks launched sessions failed; it never advertises a stale endpoint. Waiting
first-access refreshes may resume. Snapshot history remains available for new
sessions. Worker failure does not restart Postgres. Session/query status retains
bounded diagnostics, and runtime status reports pending analytical cleanup errors.

Portable tests cover durable references, admission and project scoping. Native
Linux/macOS qualification exercises real SQL and DataFrames, two retained epochs,
independent Delta reads, read-only admission, limits, CLI/shell lifecycle,
Postgres suspension, cancellation, worker death and daemon recovery.
