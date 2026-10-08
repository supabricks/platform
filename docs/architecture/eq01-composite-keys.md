# EQ01 — native composite integer keys (#170)

Status: implemented; local worker and installed native correctness checks pass.
Installed Linux/macOS release qualification remains a merge gate. This is the
first EQ01 compatibility slice; [SP stays frozen](sync-performance-freeze.md).
[Issue #170](https://github.com/supabricks/platform/issues/170) is separate from
DATE/CHAR support in [#171](https://github.com/supabricks/platform/issues/171).

## Behavior

Capture now admits a primary key containing multiple non-null `smallint`,
`integer` or `bigint` columns. It preserves every key component without surrogate
keys or schema conversion. This admits the seven native TPC-DS fact/inventory
schemas rejected for composite identity in EQ00. The repeated full admission
matrix improves from **1/24 to 8/24 accepted**; the sixteen remaining rejections
are DATE/CHAR restrictions under #171.

The source inspector distinguishes index key attributes from `INCLUDE` payload
using PostgreSQL's [`indnkeyatts`](https://www.postgresql.org/docs/17/catalog-pg-index.html).
Identity is represented in physical column order, matching
[pgoutput's per-column key flags](https://www.postgresql.org/docs/17/protocol-logicalrep-message-formats.html),
even when the primary key declares another order. Included columns are not keys.
Existing replica-identity, relation, type, schema and transaction bounds remain.
This does not admit text/date/character keys or change the schema-drift policy.

The decoder and journal retain the original complete transaction bytes. Row
overlay uses an integer tuple for composite identity, including both old and new
tuples when a key changes. A single key retains its scalar representation, so
existing single-key journals and saved plans keep their encoding. Composite plan
keys serialize as arrays and replay directly from the durable saved plan. Older
workers cannot consume a composite-key capture; do not downgrade it in place.

Affected-row lookup first prunes on each key column, then checks exact tuple
membership in 32-row batches before row/value budget accounting. Independent
column matches alone never select a row. This avoids constructing thousands of
OR terms: an initial full-batch test exposed an Arrow optimizer crash with that
approach. The failing test log is retained; the final maximum-batch test passes.
Pruning can read additional candidate rows for shared component values; measuring
that cost on the real TPC-DS dataset remains part of EQ02/EQ03. Existing planning
deadlines and retained-row/value budgets still apply.

Delta merge joins every key column. Deletes carry all identity components, and
key moves delete the old tuple and insert the new tuple as one planned table
change. Shared prefixes do not collide; duplicate complete identities and missing
source keys still fail closed. Table-set publication remains atomic and old Sail
sessions retain their pinned versions.

## Qualification

The analytical worker suite passes **138 tests**, including five new composite
tests. New coverage includes mixed integer widths/extremes, null/unchanged/invalid
keys, shared prefixes, exact pair filtering, a 32,768-key lookup, two-/three-part
identity, TOAST preservation, exact decimals, transaction coalescing, spool reopen,
duplicate journal replay and saved-plan recovery after the first real Delta table
commit. Previous versions remain independently readable after partial mutation.
The existing single-key tests pass without changing their fixtures or plan format.

The installed native test bootstraps all seven original TPC-DS composite schemas
plus populated two-/three-key fixtures and an unchanged scalar control. It tests
reordered keys with INCLUDE payload, I/U/D, key movement, PostgreSQL duplicate
rejection, integer extrema, unchanged TOAST, decimal equality, old/latest Sail
queries, capture-process SIGKILL and daemon restart without replacing bootstrap.
Those seven TPC-DS tables are empty schema probes; this is not a TPC-DS dataset or
full-query result claim. The populated fixtures exercise actual capture/apply and
Sail behavior. Final PostgreSQL and published Delta rows match exactly.

The same checks are mandatory in the installed `composite` release suite on Linux
and macOS. Release evidence rejects missing composite checks or changed source/
row-conversion worker hashes. Offline CI uses the verified committed EQ00 schema
inventory, not an unpinned external download. Local engineering-overlay checks do
not replace exact-archive release evidence.

## Measured slice and reproduction

The unchanged control uses two 10,000-row scalar-key tables, twelve transactions
changing 64 rows each, with final exact source/Delta equality. Three fresh baseline/
candidate pairs alternate execution order, with 8 CPUs (0–7), 16 GiB memory and
swap disabled. No new composite schemas are added to the scalar timing control.
Commit latency and acknowledgment-to-observed-publication latency are recorded
individually; the observer polls at 200 ms. No sustained throughput target or
speedup is inferred from this short compatibility regression screen. Host resource
isolation is limited to the container budgets, not exclusive physical hardware.

All six controls pass exact equality and cleanup. Mean observed publication latency
was **840.77 ms baseline → 875.67 ms candidate (+4.15%, +34.90 ms)** across 36
transactions per arm. The per-pair means were 860.07 → 901.68 ms, 822.23 → 866.45 ms,
and 840.02 → 858.89 ms. Retain this observed cost; it is not a speedup or proof of
no regression. Three short runs with a polling observer do not establish the
cause or a sustained performance envelope. This slice's demonstrated contribution
is native composite-key correctness and schema admission, not throughput.

[Evidence](tpcds-evidence/2026-10-06-issue170/README.md) records every attempt,
package identity, changed source hashes, cleanup and measured comparisons.
[Harness instructions](../../e2e/tpcds/README.md#eq01-composite-key-qualification)
describe reproducing the native fixture and scalar control. Runtime changes are
limited to three Python files; no Rust binary, dependency, journal backend,
durability setting or SP parameter changed.

Next: implement and independently measure DATE/CHAR support under #171, then run
the full SF1 load with sync active and the complete query suite against Sail and
the pinned Spark reference. The original EQ00 receipts remain unchanged.
