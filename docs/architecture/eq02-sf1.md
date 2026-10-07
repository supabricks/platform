# EQ02 — native SF1 loading and analytical qualification

Status, 2026-10-07: **resumed; full end-to-end qualification is not complete**.
Six completed installed load attempts are retained below; attempt 07 is running. The fourth reached the apply-worker
memory blocker (#182). The [bounded lookup correction](eq02-key-pruning.md) passes
its regression gates; attempt 05 crosses that boundary but stops on a reproducible
post-compaction Delta merge stall (#184). The [bounded Delta correction](eq02-bounded-merge.md)
passes retained failure replays, 147 worker tests and 26 installed checks. Its
first candidate exposed a join-order allocation failure in attempt 06; the complete
correction is undergoing attempt 07. Full qualification remains incomplete.
SP remains [frozen](sync-performance-freeze.md). This work does not merge its
candidate or restart its performance campaign.

The [EQ01 DATE/CHAR slice](eq01-date-char.md) admits all 24 original tables.
[Sail PR #1](https://github.com/supabricks/sail/pull/1) is merged. Platform
[#177](https://github.com/supabricks/platform/pull/177) is awaiting fresh archive
CI, including corrections to historical baseline provenance checks in
[#181](https://github.com/supabricks/platform/issues/181). Its target remains the
stacked EQ branch; this is not a merge of frozen SP into main.

## Workload and retained attempts

All attempts use the previously generated, hash-verified SF1 business files:
**19,557,335 rows, 24 tables**, unchanged PostgreSQL columns and primary keys.
Continuous sync is healthy on the empty table set before the first COPY.
One loader commits at most 1,024 rows / 4 MiB of escaped COPY input per transaction.
Each attempt has its own native cell, 8 CPUs (0–7), 16 GiB memory, no swap or
external network, a two-hour load/drain deadline and 80 GiB free-space admission
on the second NVMe. The sampled cell-storage ceiling is 64 GiB, not a disk quota.
The release, input files and harness mount are read-only. No quiet-period sleeps
or automatic replacement trials are used.

| Attempt | Change | Rows committed | Rows in last publication | Elapsed, including startup/cleanup | Outcome |
| --- | --- | ---: | ---: | ---: | --- |
| 01 | EQ01 DATE/CHAR baseline | 212,816 | 11,264 | 28.285 s | `apply_row_budget`, capture fenced |
| 02 | Complete-transaction row prefix | 2,148,347 | 238,416 | 56.200 s | Generated Latin-1 country rejected as UTF-8 during COPY |
| 03 | Explicit Latin-1 import | 3,322,145 | 319,312 | 74.347 s | `wal_budget`, capture fenced |
| 04 | Publication-window flow control | 1,301,328 | 1,235,792 | 508.166 s | `incremental_memory_budget`, capture fenced |
| 05 | Bounded key lookup | 1,953,616 | 1,888,080 | 539.224 s | Post-compaction merge stalls; request expires (#184) |
| 06 | Single-partition-only Delta candidate | 221,008 | 155,472 observed | 38.340 s | Optimizer builds hash from target; unchanged pool exhausted |

These are failure-discovery and harness pilots, **not comparative throughput
qualification**. The completed attempts stop at different data boundaries; their
elapsed times cannot establish a speedup. Commit ledgers retain every attempted
and acknowledged batch, offsets, row/byte counts and COPY-plus-commit latency.
Observed publications are distinct from source acknowledgment. Counts above do
not assert source/Delta equality: source was ahead when these attempts stopped.
All six completed attempts stopped with zero leaked/remaining descendants.

Receipts, compressed logs, exact earlier loader sources and package proof are in
[the evidence directory](tpcds-evidence/2026-10-07-eq02/README.md). These trials use
verified **unsigned engineering overlays**, not exact signed-release archives.

## Separately identified issues

[**#178 — aggregate row work**](https://github.com/supabricks/platform/issues/178):
the journal admits up to 16 MiB, which can contain more than 16,384 small changes
across individually valid source transactions. The applier previously rejected
that combined batch and fenced capture. Commit `94474db` selects a complete
transaction prefix within the existing row limit, records only that prefix's LSN
and input bytes, and drains the remainder later. A single oversized transaction
still fails closed; no transaction is split. Tests cover multiple tables, key
moves, saved-plan crash/replay, old readers and rejection before mutation.
All 144 analytical worker tests pass. The installed bulk regression also passes:
24,576 changes from twelve valid transactions drain through multiple apply
batches with exact source/Delta equality and a pinned old reader; a 20,000-row
single transaction fails without replacing the good publication. This is now a
mandatory Linux/macOS archive gate. Attempt 02 crosses the previous failure
boundary; no full-scale performance claim follows from that improvement.

[**#179 — input encoding**](https://github.com/supabricks/platform/issues/179):
the pinned toolkit's `tools/tpcds.dst` contains Latin-1 country names. Customer
row 28 contains byte D4 in `CÔTE D'IVOIRE`; treating it as UTF-8 is invalid.
The [load profile](../../e2e/tpcds/load-profile.json) freezes ISO-8859-1 decoding
for both PostgreSQL COPY and independent Spark input. Original generated bytes
and checksums remain unchanged. Attempt 03 verifies the actual non-ASCII value
after PostgreSQL import. No replacement characters, trimming or regeneration.

[**#180 — source flow control**](https://github.com/supabricks/platform/issues/180):
unrestricted COPY outruns capture. Attempt 03 reported 423,312,288 retained WAL
bytes at the existing 80% safety boundary of its 512 MiB limit. The fourth pilot
pauses outside source transactions when committed rows exceed the coherent
publication by 65,536. This is an insert-only workload with one writer and an
empty initial source, so exact publication row totals provide the acknowledgment
window. Waiting time is included in achieved ingestion time and retained separately.
This does not qualify arbitrary concurrent writers, update streams, resumable
uploads or an expanded WAL budget. Attempt 04 controlled WAL successfully but
did not complete the dataset.

[**#182 — apply-worker memory**](https://github.com/supabricks/platform/issues/182):
the fourth attempt crossed the daemon's 768 MiB limit for a busy reusable apply
worker. It was not the 16 GiB container ceiling. Final retained WAL was just
1,512 bytes and journal size approximately 12.96 MB. The loader recorded 464.290
seconds of flow-control waiting. The previous successful apply read its journal
in 23 ms. The [follow-up investigation](eq02-apply-memory-investigation.md)
reproduces a fresh-process 785.8 MiB peak: the planner unnecessarily scans
1,185,792 existing rows before Delta merge adds further memory. Explicit key
bounds plus bounded scanner read-ahead reduce retained-range median plan/apply
time from 8.208 s to 0.653 s, with 486.5–541.7 MiB peaks across three candidate
runs and identical plans/added rows. The correction is now implemented in
`e36f07c`; 147 worker tests and six installed suites pass. Attempt 05 passes the old failure point with observed apply peaks
below 678 MiB, then stops on #184. Worker limits are unchanged, and #182 remains
open pending complete qualification. The original diagnostics and all controls
remain retained. See [implementation and rerun evidence](eq02-key-pruning.md).

## Exact data and analytical coverage gates

The harness now includes a separate post-load verifier and an Apache Spark JVM
reference runner. The independent Spark reference completed **103/103 statements**, returning
11,637 fully consumed rows across the suite. SQL elapsed totals 87.853 seconds;
reference loading took 15.502 seconds and the full reference run 103.741 seconds.
It used Spark 4.2.0, Python 3.12.13 and the captured bundled Java runtime
17.0.20.1+1. Cleanup observed ten descendants with zero leaked/remaining. This is
one reference execution, not a comparative performance result or a passing
product/reference comparison. The full product verifier is blocked on the
incomplete load (#184); #182 qualification remains open. Ten harness tests pass,
including COPY framing, partitioned
comparison, duplicate/null/padding preservation and conservative query verdicts.
Implementing a runner does not qualify its full SF1 results.

`verify.py` requires a successfully loaded, stopped cell with the same installation
identity. It resumes that private cell, pins the completed publication, compares
all source rows with the corresponding Delta versions using bounded canonical
SHA-256 partitions, and preserves duplicates, nulls, exact decimals, DATE and
padded CHAR values. It also checks Delta types and CHAR metadata. A digest
mismatch blocks query qualification and requires row-difference investigation.
Counts alone are insufficient.

Each of the 103 pinned SQL statements then runs through the product-owned managed
Spark Connect session and consumes its complete result. There is no COUNT wrapper
or added LIMIT. Session startup is recorded separately, and each statement has a
120-second execution ceiling. A 16 MiB evidence ceiling fails explicitly instead
of accepting a truncated preview. A fresh session does not imply cold OS caches.
The product's own worker memory, spill and lifetime limits remain in force.

`reference.py` runs pinned Apache Spark 4.2.0 JVM separately from timed product
work, imports the same immutable native logical data using real Spark CHAR table
semantics, and attempts the entire suite. Per-query schemas, complete rows, plans,
timings and failures are retained. `compare.py` binds both results to the same
input, generation and encoding receipts and compares positional SQL types and
values exactly. There is no retrospective floating tolerance. Equal multisets
with different ordering require explicit ORDER BY/tie review; different LIMIT
boundary results also remain review-required. No subset is labeled full coverage.

Current **product analytical coverage is 0/103**. Full exact product data verification,
product query execution/comparisons, console upload acceptance, larger scales
and concurrency/recovery experiments remain pending under the
[end-to-end plan](../plans/tpcds-end-to-end-qualification.md).
