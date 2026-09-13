# CSV and TSV ingestion (I01)

For JSON, JSONL and Parquet, see [file ingestion](file-ingestion.md).

The native analytical preview includes a private CSV worker using the already
pinned PyArrow and Psycopg dependencies. No host Python or pip installation is
needed. JSON and Parquet support, and the browser file picker, follow in I02/I03.

From an initialized project with a running `main` database:

```sh
supabricks ingest inspect ./orders.csv > inspection.json
```

Inspect the sample and copy `inspection.mapping` from this response to
`approved-schema.json`. Review every target name, type and nullability before
loading. The default proposal uses text so identifiers such as `001` and large
numbers retain their original values. For example, change a column's
`data_type` to `{"kind":"bigint"}` or
`{"kind":"decimal","precision":20,"scale":2}` when that conversion is intended.
`input` is the zero-based source column index as a string; retain each input
exactly once. Duplicate/empty headers receive distinct proposed target names.

```sh
supabricks ingest load --source SOURCE_ID --branch main --schema public \
  --table orders --schema-file approved-schema.json --key orders-first --wait
supabricks ingest status JOB_ID
supabricks ingest list --branch main
supabricks sql --branch main --sql 'SELECT count(*) FROM orders'
```

`SOURCE_ID` comes from `source.id` in the inspection response. `load` without
`--wait` returns the accepted job immediately. `load ./orders.csv` can stage a
file using an already approved schema file. Its idempotency check compares the
newly staged bytes with the original request. Changed content or mapping with
the same key conflicts. A retry of the same request returns the original job.

Supported parsing is UTF-8 (optional BOM), comma/tab/semicolon/pipe delimiters,
doubled quotes and quoted newlines. A `.tsv` inspection defaults to tab; use
`--delimiter` to override and `--no-header` for positional columns. The default
null-token list is empty: empty fields remain empty text. Pass
`--null-strings '[""]'` to interpret an unquoted empty field as null; quoted
`""` remains empty text. The approved mapping freezes these choices.

Scalar types are text, boolean (`true`/`false`), smallint/integer/bigint, decimal
(up to precision 38), finite double, ISO date, timestamp and timestamp_tz.
Integers and decimals reject overflow; decimals reject rounding. Timestamps
require seconds, at most six fractional digits, and the selected timezone
semantics. SQL identifiers are quoted, including spaces or embedded quotes.
Analytical export retains its existing Delta column-name restrictions; rename
unsupported PostgreSQL column names before refreshing analytics.

Imports create **new tables only**. Table creation, streamed COPY and the internal
receipt commit in one PostgreSQL transaction. A preview validates only its
sample; a bad later row rolls the entire table back. `copied_rows` measures work
sent to COPY and does not mean committed data. Only a `succeeded` job has a
`committed_rows` count confirmed against its origin-scoped PostgreSQL receipt.

```sh
supabricks ingest cancel JOB_ID --wait
supabricks ingest retry JOB_ID --wait
supabricks ingest dispose SOURCE_ID
```

Cancellation fences the worker first. A commit already in flight can still
succeed. `reconciling` means its outcome remains unresolved: keep the origin
branch available and poll status. Never submit another key to bypass it. Retry
is explicit and allowed only after proven rollback, while the unchanged source
and branch revision remain valid. Worker loss does not automatically reload.
Stopping the cell waits for fencing and receipt resolution before stopping
PostgreSQL; an unavailable origin can leave shutdown pending.

One import and one source inspection can run concurrently per cell. Limits are
100 MiB per source, 512 MiB staged payloads, 512 MiB decoded values, 512 MiB sampled
worker RSS, 256 columns, a 1 MiB parser block/record budget and ten minutes per
worker. Some valid records near the block limit can be rejected by the parser.
The preview has at most 100 rows and 256 KiB. Workers check a 64 MiB free-space
reserve; this is an admission/abort margin, not a guarantee that PostgreSQL WAL
and table storage will fit. A watchdog samples RSS every 50 ms; transient peaks
between samples are not a hard OS memory quota.

Staged files and previews are private. Successful/cancelled jobs release payloads
when no other retained request needs them. Unused/failed sources expire after
24 hours; explicit disposal revokes retries. Preview samples disappear on daemon
restart or disposal. Durable job identity, mapping, hashes and committed counts
survive restart. Raw rows and connection credentials are excluded from job
history and ordinary error messages.

The nine `ingest_*` MCP tools use this same project-bound service. Inspection
accepts an explicit absolute device path, and returns immediately; poll
`ingest_source`. Supply the full approved `load` object including project/branch
UUIDs, revision and source hash. Agent write authorization must name the target
branch and table. Browser filesystem access is not exposed in I01.
