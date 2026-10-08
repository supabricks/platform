# EQ02 — apply memory investigation (#182)

**Follow-up:** the correction is now implemented; see [packaged checks and SF1 attempt 05](eq02-key-pruning.md). The text below records the preceding diagnostic investigation.

Investigated 2026-10-07 against the retained SF1 load-04 failure. **Unnecessary
Parquet scanning and the memory retained into Delta merge are a demonstrated
cause of excessive memory use.** A fresh-process replay crossed 768 MiB, so
worker reuse is not necessary to reproduce that breach. This does not establish
an allocator leak or exclude additional costs in the full worker lifecycle.

The candidate changes below exist only in the diagnostic harness. Production
code and its memory limits are unchanged. [#182](https://github.com/supabricks/platform/issues/182)
remains open pending implementation and installed SF1 qualification. SP stays frozen.

## What the failed range does

The retained publication has 1,185,792 `customer_demographics` rows in Delta
version 19. The next complete-transaction prefix inserts 16,384 rows with keys
1,185,793 through 1,202,176, ending at LSN `0/D6A8B00`. No existing key matches.

The current lookup uses Arrow `field.isin(keys)` and 32-row scanner batches.
Inspection of the actual Parquet fragments demonstrates:

| Lookup predicate | Files presented to Arrow | Selected row groups | Existing rows in selected groups |
| --- | ---: | ---: | ---: |
| No filter | 20 | 1,158 | 1,185,792 |
| Current `isin` | 20 | 1,158 | 1,185,792 |
| Same `isin` plus inclusive key minimum/maximum | 20 | 0 | 0 |

The current predicate produces **37,056 empty batches**. Each batch invokes the
planning ownership/deadline/storage guard, for **37,084 guard calls** overall.
A separate profiled run recorded 2,043,504 `stat` calls, including 2,040,268
`Path.lstat` calls. Its instrumented guard accumulated about 8.7 seconds.
Profiler timings are not included in the comparison below, and overlapping
thread timings must not be summed into a wall-time breakdown.

Default Arrow scanning also reads ahead across batches and fragments, with
threaded execution. At the end of baseline planning, RSS was about 447–457 MiB.
Delta merge then increased the process footprint further: one baseline ended
at **786.1 MiB RSS**, with **785.8 MiB kernel high-water RSS**. Ten 5 ms samples
above 768 MiB spanned 55.7 ms during/after apply; the final post-GC RSS remained
above that boundary. The diagnostic container deliberately allowed 2 GiB, so
the operation completed without a daemon enforcing the 768 MiB worker limit.

Arrow's default pool reported zero outstanding bytes after planning and again
after apply, while process RSS remained high. Its reported planning peak was
only about 17 MiB. Therefore that pool counter alone does not describe the
process budget: native allocations, retained allocator memory and other pools
need to be considered. These measurements do **not** identify a specific leaking
allocator. Python garbage collection alone did not release the footprint.

## Controlled changes and measurements

Each run starts a fresh process and independently copies the same journal and
generation. Original state is mounted read-only. All use CPUs 0–7, a 2 GiB
container with no swap/network, the same packaged worker and the same 5 ms RSS
sampler. The measured interval covers journal read, planning and apply; it
excludes imports/state copying. No concurrent local build or product workload
was launched during these trials. Remote GitHub CI is separate.

Four initial pilot runs are retained as trial 01. The comparison uses trials
02–04, all run with the final diagnostic script, sequentially in the order below.
This is a warm-host diagnostic, not a randomized or cold-cache performance campaign.

| Variant | Plan/apply seconds, median of 3 | Kernel peak RSS across 3 runs | Empty batches / guard calls |
| --- | ---: | ---: | ---: |
| Current implementation | 8.208 | 713.5–785.8 MiB | 37,056 / 37,084 |
| Bounded scanner only | 10.149 | 530.5–548.4 MiB | 37,056 / 37,084 |
| Explicit key bounds only | 0.721 | 480.1–567.8 MiB | 20 / 48 |
| Key bounds + bounded scanner | 0.653 | 486.5–541.7 MiB | 20 / 48 |

“Bounded scanner” sets `batch_readahead=1`, `fragment_readahead=1`, and
`use_threads=False`; the existing 32-row materialization bound stays intact.
“Key bounds” adds `field >= min(keys)` and `field <= max(keys)` alongside each
key column's existing membership predicate. Composite keys still require exact
tuple filtering after candidate pruning. No guards, row limits, transaction
boundaries, merge semantics or memory limits are removed.

Read-ahead limits reduce memory but slow this unpruned scan. Key bounds address
the unnecessary scan itself and remove its empty-batch guard overhead. The
combined candidate is about 12.6 times faster **for this retained insert range**;
that is not an ingestion-rate or full SF1 speedup claim. Merge still scans 20
target files according to its own metrics and remains a material memory cost.
The planner's Parquet pruning change does not change Delta merge's predicate.

All 16 pilot/comparison runs produce the identical plan SHA-256
`9877790a8fd5e7c4f1b4e2d816fa3ba810316cb90d360714c355d81de60b43ea`,
row count and end LSN. A separate untimed check compares all 16,384 added rows
and Arrow schemas, including metadata, across every run; all match exactly.
Original Parquet files remain byte-identical and no commit removes old files.
This is diagnostic equivalence, not full PostgreSQL/Delta equality or coverage
of updates, deletes, key moves, missing statistics or unsorted data.

## Recommended correction and acceptance

1. Add inclusive per-column key bounds to the existing membership predicates.
   Keep the exact composite identity filter and previous-version read semantics.
2. Bound planner read-ahead and threading as above so overlapping/unsorted keys
   do not depend on successful pruning to control memory. Keep the 32-row batch
   size, mutation checks, deadlines and current daemon limits.
3. Add real-Parquet regressions for nonmatching ranges, missing statistics,
   sparse keys, integer extremes and composite Cartesian neighbors. Exercise
   updates, deletes, key moves, saved-plan replay and old pinned readers.
4. Package a verified engineering overlay and rerun the installed bulk/type/
   recovery checks plus the identical SF1 loader with the 65,536-row publication
   window. Observe full-worker peaks, reuse and compaction under the actual
   768 MiB gate. Retain any subsequent failures as separate evidence.
5. Close #182 only after that load crosses the old failure point and completes
   its required acceptance. Full EQ02 additionally requires source/Delta
   equality and all 103 analytical statements against the independent reference.

Increasing the memory cap is not needed to test this correction. Neither the
profile nor these interventions implicates SQLite journal contention: the
diagnostic journal reads take about 10 ms, and the growth occurs after decoding.
Adding RocksDB or FoundationDB would not eliminate this demonstrated Parquet
lookup and merge work.

## Evidence and reproduction

[Retained artifacts](tpcds-evidence/2026-10-07-eq02/issue-182/summary.json) include
all results, 5 ms samples, raw logs, predicate-pruning inspection, equivalence
checks and the separate profile. Source scripts live alongside them and are
covered by the parent `SHA256SUMS`. The worker is the existing row-prefix overlay
`c707145b075a26682765bddf31dc8833eda03f024c31906575a60f5d41578509`.

The retained shell runner records the exact container and mount configuration.
Restore its Python script under `build/eq02-20261007/`, retain that verified
runtime and the original private load-04 state, then run each mode with a fresh
trial suffix. The runner refuses existing output directories. Private databases,
launch tokens and source credentials are not public artifacts. A fresh machine
must reproduce load-04 before it can use this retained-state replay.
