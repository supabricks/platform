# TPC-DS end to end qualification

Status: **EQ02 resumed 2026-10-07; EQ00 started 2026-10-06. Prioritized ahead of further SP work by user
direction.** The [SP workstream is frozen](../architecture/sync-performance-freeze.md)
at its retained results, including unresolved #169. SP11/SP12 completion is no
longer a prerequisite for starting EQ. Return to SP after reviewing end-to-end
results. No SP qualification or tuning runs alongside this work.

[EQ00 initial results](../architecture/tpcds-eq00.md): sources and all 103 SQL
statements pinned; SF1 generates 19,557,335 business rows / 1.253 GB with matching
checksums across two invocations. Installed native-schema admission rejects 23/24
tables; [#170](https://github.com/supabricks/platform/issues/170) and
[#171](https://github.com/supabricks/platform/issues/171) track composite keys and
DATE/CHAR support. Product/reference execution remains pending; no scale claim.

[EQ01/#170](../architecture/eq01-composite-keys.md) implements native composite
integer identity and passes local installed correctness/restart/Sail checks. The
first admission matrix reached 8/24. [EQ01/#171](../architecture/eq01-date-char.md)
now admits and bootstraps all 24 native schemas with finite DATE and padded CHAR
payloads. Local installed restart, historical reads and typed query checks pass
against PostgreSQL and independent Spark 4.2.0 results. The full SF1 load and
103-statement analytical suite remain pending; Spark Connect DataFrame metadata
round trips are tracked separately in #175. Nine separate-slice scalar controls
pass equality/cleanup; source-commit stalls remain tracked in #176. The measured
publication means are 857.82 → 881.88 → 843.77 ms (baseline → DATE → CHAR);
this short screen is not a speedup or sustained performance qualification.
The composite slice's six scalar control trials pass equality and cleanup; mean observed publication
latency increased 4.15% in the short screen. Exact release CI remains a merge gate.

[EQ02 SF1 attempts](../architecture/eq02-sf1.md) have now exercised native loading
with continuous sync already active. Separate retained attempts found and tracked
aggregate apply row limits (#178), Latin-1 generator text (#179), and source WAL
overrun under unrestricted COPY (#180). The publication-windowed load controls
WAL but initially stopped at the apply worker's 768 MiB memory limit (#182).
The [bounded key-lookup correction](../architecture/eq02-key-pruning.md) passes
147 worker tests and six installed suites. Attempt 05 crosses that failure point
with observed apply peaks below 678 MiB, then stalls in post-compaction Delta
merge (#184), also reproduced with the pre-fix runtime. The
[bounded Delta correction](../architecture/eq02-bounded-merge.md) now passes
retained replays, 147 worker tests and 26 installed checks; its complete
single-partition/source-first implementation crosses the old failure boundaries after
the first candidate exposed a target hash-build allocation failure in attempt 06.
Attempt 07 crosses both merge failures but exposes a temporary-file rename race
in compaction (#185); a bounded live-inventory retry passes 150 worker tests and
installed recovery/continuous checks. Attempt 08 reaches 12.75M committed rows
then exhausts its two-hour deadline. [Publication scheduling #186](../architecture/eq02-publication-verification.md)
accounts for most of the measured delay; its correction passes matched daemon
measurements and all 12 installed checks; attempt 09 is running.
Full qualification remains open.
The independent Spark reference completes all 103 statements; full product
table equality, product query execution and result comparisons remain pending.

The initial engineering baseline is platform source
`8b68cd206edd5de2b1f820c90c39e7a76aedf3aa`, whose full Linux/macOS release CI passed
in run `37522400869`. Exact package identity must accompany each installed trial.
The EQ branch is stacked on that unmerged SP candidate; this does not merge PR
#167, qualify sustained performance or transfer SP's targets to TPC-DS.

The objective is to measure the complete product journey: load a substantial
retail dataset into PostgreSQL with continuous sync enabled, query the resulting
analytical tables, and establish how correctness, freshness, query latency and
resource use change with data volume and concurrent activity. Existing SP tests
use two narrow, 10,000-row tables; their results do not establish this envelope.

## Product path and query engine

The measured path is generated files → supported upload/import or PostgreSQL
loader → logged PostgreSQL tables → continuous capture and publication → catalog
and pinned analytical session → Spark SQL queries and fully consumed results.
Generated Parquet registered directly in analytics does not exercise this path.

The current product executes analytics with Sail through Spark Connect. Use its
actual console/notebook/SQL session path for product acceptance. Also run a pinned
Apache Spark reference against equivalent immutable data to verify SQL semantics.
Record these engines separately; a Sail measurement is not an Apache Spark engine
measurement. Reference execution runs separately from timed product trials unless
the profile explicitly measures contention.

## Dataset and SQL coverage

Pin the TPC tool version, generator source/binary hashes, seeds, generation
arguments, PostgreSQL DDL and query parameters. Preserve the complete warehouse
schema, including fact and dimension tables, nulls, decimals, dates, text,
relationships and generated distributions. Verify table inventories, row counts
and file checksums before loading. Record any schema or dialect adaptation.

TPC-DS includes complex decision-support SQL and data maintenance. Its store-sales
key is composite, and scale-factor labels describe approximate generated raw data,
not PostgreSQL or compressed analytical storage. This plan uses a **TPC-DS-derived
engineering workload**, without claiming an official TPC-DS score. The proposed
small scales are not official performance-result scales. See the
[TPC-DS specification, clauses 2–5](https://tpc.org/TPC_Documents_Current_Versions/pdf/TPC-DS_v4.0.0.pdf).

Attempt the full 99-template query suite, including every statement when a
template produces multiple queries. Freeze SQL variants and substitutions in a
manifest; report template and executable-statement counts separately. Coverage
must include large fact/dimension joins, cross-channel sales and returns,
profitability, customer segmentation, inventory, subqueries, window functions,
rankings and complex aggregation. Tiny substitutes do not satisfy coverage.

Each query gets a result: correct, incorrect, unsupported, timeout, resource
failure or not run with a reason. Unsupported or failed queries remain in the
denominator and get linked issues. Do not publish a full-suite claim for a passing
subset. Spark's own [TPC-DS test source](https://github.com/apache/spark/blob/master/sql/core/src/test/scala/org/apache/spark/sql/TPCDSBase.scala)
contains exclusions and version-specific variants; pin the selected source and
inspect its complete inventory rather than inheriting its exclusions silently.

## Compatibility before scale

The EQ00 baseline [capture profile](../architecture/sy02-durable-capture.md)
admitted one integer primary key and a bounded integer/text/varchar/decimal type
set. EQ01 adds qualified composite integer keys and finite DATE/padded CHAR.
Schema admission alone does not establish full-load eligibility. Inspect the selected release and
create a per-table compatibility report covering composite keys, dates, character
semantics, numeric precision, nullability, schema size and transaction limits.

Implement and qualify required support in separately measured slices before
claiming native-schema end-to-end coverage. Test bootstrap, insert/update/delete,
key changes, replay, restart and exact analytical types. An exploratory surrogate
key or type conversion must be labeled an adaptation, preserve logical query
semantics, and keep the native-schema gap open. Do not silently flatten schemas,
drop columns, convert exact money values to floating point or bypass sync.

Audit upload format handling, chunked commits, cancellation and retry semantics,
session lifetime, result limits and reader slots. The current
[analytical workspace](../handbook/analytical-workspace.md) documents two shared
session slots and pinned epochs; increased concurrency may require a product slice.
File upload into existing enrolled tables may also require an ingestion extension.

## Proposed scale and resource matrix

| Scale factor | Approximate raw input label | Purpose |
| --- | --- | --- |
| SF1 | 1 GB | Full schema/query correctness and harness qualification |
| SF10 | 10 GB | First substantial product workload and resource baseline |
| SF100 | 100 GB | Larger working set, storage growth, spill and saturation |

These are planning sizes, not measured footprints. Generate and record actual
bytes and rows. Admit each scale only after budgeting generated files, PostgreSQL
and indexes, WAL retention, journal backlog, Delta versions, query spill, reference
data and evidence. SF100 may require additional storage. Never delete retained SP
evidence to make room or assume the raw input size equals required disk space.

First hold CPU/RAM fixed while increasing dataset size. Then repeat selected
qualified cells at 4, 8 and 16 CPUs with a fixed memory cap and recorded SMT/NUMA
placement. Vary memory separately to expose cache/spill effects. CPU affinity on
this workstation is not evidence of equivalent EC2 instance performance.

Start with one and two analytical streams, sharing the declared whole-stack
resource budget. Add four streams only after session capacity is implemented and
qualified. Freeze a staged matrix after pilot timings rather than multiplying
every size, CPU, memory, query and load setting into an unbounded campaign.

## Experiments

| Experiment | Activity | Question |
| --- | --- | --- |
| Initial load | Create/enroll empty tables, enable sync, then load in bounded committed batches | How long from starting upload to a complete queryable dataset, and how much backlog accumulates? |
| Upload acceptance | Use the real console import path with declared file sizes/formats | Can a user load data, observe progress, handle a failure and continue correctly? |
| Query baseline | Full suite over a complete pinned publication, no writes | Which SQL works and what are its latency and resource costs? |
| Sync overhead | Matched source loads with sync off/on on fresh identical installations | What does sync cost PostgreSQL ingestion throughput and transaction latency? |
| Concurrent analytics | Same load or refresh stream with zero, one and two query streams | What do queries cost freshness and ingestion, and what do writes cost query latency? |
| Continued changes | Deterministic inserts, updates and deletes after bulk load | Do correctness and bounded resource use survive ongoing change? |
| Recovery under load | Controlled loader disconnect, capture/apply restart and query cancellation | Can the system recover without loss, duplication or a mixed publication? |

Use bounded COPY/import transactions with fixed rows and byte limits; do not use
one giant transaction whose visibility starts only at final commit. Measure loader
capacity independently. Preserve unsent work and achieved rows/s when a source
misses its offered rate. Include a declared oversized-transaction rejection case.

Use the pinned generator's maintenance data where practical. Define any additional
insert/update/delete mixture and hot-key distribution explicitly as a custom
change workload. Compare both a fixed absolute rate (initial target 1,000 changed
rows/s) and a separately declared fraction of dataset changed per unit time.
Record transaction sizes, committed bytes/s and changed rows/s; these are different
dimensions. Under full-speed initial load, measure time to drain separately from
steady-state freshness.

## Correctness and freshness

After initial load and at defined change checkpoints, stop source writes at an
identified committed boundary, drain to the corresponding publication, and pin
that epoch for verification. Compare PostgreSQL with all published tables using
bounded, canonical, partitioned comparisons; counts alone are insufficient.
Preserve duplicate multiplicity, nulls, exact decimals, dates and string semantics.
Escalate partition digest mismatches to row differences.

Verify complete query results against the independent Spark reference using the
same logical source boundary. Preserve ORDER BY semantics and handle unspecified
ordering and boundary ties explicitly. Declare any floating-point tolerance before
measurement; do not round away exact-decimal errors. Ensure SQL actions execute
and consume complete output: lazy plan creation, COUNT wrappers, UI truncation
and LIMIT previews cannot stand in for the intended query.

An existing analytical session intentionally stays on its pinned epoch. Measure
new-epoch availability separately from time to open/rebind a latest session and
time until a completed query returns the expected change. Record source commit
LSN/acknowledgment, published epoch, query start/end and result boundary. Account
for observation resolution and cross-host clock error. Never compare a changing
source to an older session and call the difference lost data.

Use one coherent table-set epoch for multi-table joins. Queries during partial
initial load describe that recorded partial boundary; full-dataset correctness
requires completion and drain. Run final correctness checks outside throughput
windows unless their overhead is an explicitly measured workload.

## Measurement and review

Record per-query planning and execution time where available, full result latency,
first-use and warm behavior, throughput per stream, failures and exact query plans.
Report session startup separately. A fresh session alone does not prove cold OS
or object caches; disclose cache state and avoid disruptive host-wide cache drops.

For the whole stack and each process, record CPU, peak RSS, read/write bytes,
disk and memory pressure, spill, source WAL retention, journal backlog, analytical
file/version growth and reclamation after readers close. Track ingestion and sync
p50/p95/p99, maximum lag and drain time. Keep source acknowledgment-to-publication
and acknowledgment-to-query-result metrics separately named.

Pilot each stage, then freeze repetition counts, timeouts, query order/seeds,
load mix and resource budgets. Use at least three fresh runs for comparative
claims; distinguish those from repeated queries inside one run. Do not infer a
credible per-query p95/p99 from three observations. Publish individual results and
sample counts, plus suite elapsed time and completion coverage. Retain every
timeout and failed attempt. Use SP's continuous quiet-host evidence and bounded
contention replacement policy, while treating intended query/load concurrency as
part of the experiment.

After each logical slice, rerun its affected paired baseline/candidate cells and
record improvement, regression or no measurable contribution before proceeding.
Preserve workload identities; a compatibility-driven schema change starts a new
comparison baseline. Open repository issues for reproducible defects with source
pins, minimal reproduction and evidence.

## Delivery slices

| Slice | Deliverable and exit |
| --- | --- |
| EQ00 | Pin tools, schema and full query inventory; measure pilot time/storage; freeze acceptance thresholds, matrix and issue-backed compatibility gaps. No scale claim. |
| EQ01 | Qualify required schema, type, loader and session support. Measure each logical change independently; all intended tables enroll without hidden adaptation. |
| EQ02 | Complete SF1 load with sync already active, exact table verification and full query correctness through product and reference paths. Gaps remain explicit blockers to full coverage. |
| EQ03 | Execute SF10 then capacity-admitted SF100; qualify initial load and full analytical suite with fixed resource profiles and repeatable results. |
| EQ04 | Measure concurrent analytics plus bulk loading/continued changes, separate data/core/memory scaling, and publish the achieved freshness/throughput envelope. |
| EQ05 | Validate sustained operation and controlled recovery at the established scales, including pinned readers, retention, cancellation and resource reclamation. |
| EQ06 | Review evidence and ship a reproducible installed workflow, substantial analytical notebook, query coverage ledger, scale curves and operator guidance. |

Completion means correct full-dataset results, visible disposition of every query,
reproducible measurements at every claimed scale and an explicit supported envelope.
The prior SP target of 1,000 changed rows/s and five-second p95 publication lag is a
starting target for the steady-state test, not a guarantee transferred to TPC-DS.
Freeze query latency and resource acceptance thresholds after the pilot, before
qualification. Publish limitations and failed cells alongside successful ones.
