# Synchronization performance comparison contract

SP00 promotes the profiling experiment into a supported paired runner. The
[implementation plan](../plans/sync-performance-implementation.md) defines the
slices and acceptance gates; the [runner instructions](../../e2e/native/performance/README.md#paired-slice-comparisons-sp00-onward)
define invocation, evidence and failure handling. This document records the
measurement boundaries that subsequent optimizations must preserve.

The SP00 runtime and workload are unchanged. The predecessor is the committed
original profiling harness; the candidate adds orchestration, explicit cells,
identity validation, host monitoring and comparison accounting. Both execute the
same diagnostic package and unchanged trial/profiler code. Host monitoring runs
around both arms. There is no new runtime probe whose cost could masquerade as a
runtime gain. Original activation-control results remain in the original archive;
new runtime instrumentation will require fresh matched controls.

The default experiment uses four cells and three repetitions per arm: 4 and 16
logical CPUs at 50 offered rows/s; 8 and 16 at 1,000. Each transaction changes one
row in each of two 10,000-row tables, using four source clients, a five-second
source-only baseline, five-second warmup and catchup, 45-second load and up to
120-second drain. Complete SMT pairs and a 16 GiB no-swap envelope constrain the
whole stack, including observation. No CPU quota is set. These are local CPU
restrictions, not EC2 equivalence or a claim that four clients deliver 1,000 rows/s.

For each arm, complete latency requires attribution of every measured source
transaction and full equality of both published tables with the frozen source.
Runtime failure retains available source, backlog and profile measurements but
has no complete latency. Component metrics describe their selected windows:
capture snapshot deltas exclude unsampled edges, and successful apply-worker
medians exclude failed workers. Inclusive stage durations overlap and cannot be
added. Reanalysis applies the same formulas to the immutable original archive;
the snapshot-window selection can differ from an older narrative's rounded
numbers. Missing observations are null with counts, never invented zeros.

## Counters required before later optimizations

The predecessor currently exposes transaction append counts, SQLite COMMIT time,
and native sync calls. Its one-source-transaction-per-append behavior permits a
current syncs/commit measurement. It does not separately instrument checkpoint
work or a journal contention retry policy. The report records those capabilities
explicitly rather than presenting absent future mechanisms as zero cost.

Before judging grouped capture, add committed groups, transactions/bytes per
group, group wait time and durable acknowledgement boundary. Before judging WAL
checkpoints, add checkpoint mode, duration, busy result and frames advanced.
Before judging journal retries, add attempts, busy codes, cumulative sleep,
exhaustions and final outcome. Preserve fixed labels, no SQL/row/credential data,
and activation controls on unchanged behavior. Counters must distinguish work
attempted from durably committed and retry cost from permanent failure.

## Bounded long-run profile design

The current 45-second protocol retains one-second cumulative worker snapshots,
200 ms PostgreSQL wait samples and roughly one-second process/storage samples.
Worker output has an 8 MiB limit, and the collector currently accumulates samples
in memory. SP00 does not qualify that collector for hour-long experiments. A
longer duration must fail on its existing limits; disabling profiling would break
the comparison contract.

Before the planned sustained-load gate, implement the following format as a
separate measured instrumentation change:

1. Stream collector samples to numbered compressed chunks, rather than retaining
   the entire run in memory. Use explicit compressed and decompressed budgets,
   bounded queues and a fail-closed writer. Never overwrite old chunks.
2. Rotate worker snapshot files with a manifest containing process identity,
   sequence range, first/last monotonic and wall timestamps, byte count and hash.
   Preserve cumulative counters and fixed histogram buckets across rotations;
   compute deltas without resetting or summing cumulative snapshots twice.
3. Retain phase boundaries, every first/final snapshot and error event. For older
   interior intervals, aggregate sample counts, observed duration, minima/maxima,
   sums and histogram buckets into fixed time windows. Preserve gap/omission
   counts; do not call sampled maxima exact peaks or bucket bounds exact quantiles.
4. Finish with a manifest listing every chunk and expected stream. Missing chunks,
   sequence gaps, counter regression, write failure, budget exhaustion or absent
   daemon final snapshot invalidate measurement. Supervisor-terminated Python
   tails remain explicitly incomplete, with their last observed time.
5. Size and declare budgets before each experiment, then measure probe activation
   overhead against the same runtime. Retain a 45-second bridge comparison before
   an hour-long run, so changed aggregation does not hide a regression.

Host monitoring already implements bounded segment rotation independently of
runtime profiling. Its budget covers waiting as well as trials. Host records omit
argv and unrelated process paths; local private logs are excluded from export.

## SP00 contribution ledger

SP00 is implemented and measured in [PR #95](https://github.com/supabricks/platform/pull/95).
The [report and contribution ledger](sp00-reproducible-comparisons.md) retain 24
mandatory trials plus six predeclared follow-up trials. Decision: **Keep —
reliability/enabling**, with no runtime speedup or latency-equivalence claim. The
original low-load freshness miss and overload failures remain visible.

## SP01 contribution ledger

[SP01's report](sp01-journal-contention-recovery.md) retains the 24 mandatory
trials, contended replacements, activation controls and a predeclared low-load
follow-up. Bounded read-only retries eliminate the four observed overload resync
failures; all six overload trials still time out. Decision: **Keep —
reliability/enabling**, with no throughput improvement established. The
[cumulative ledger](../plans/sync-performance-implementation.md#attribution-decisions-and-reports)
keeps SP00 and SP01 contributions separate.

## SP02 contribution ledger

[SP02's report](sp02-durable-capture-groups.md) retains 24 mandatory trials,
24 profiler controls and 33 durable component trials. Bounded FULL/DELETE groups
reduce overload sync calls per captured transaction by about 93.4%; capture rises
from about 30 to 343–344 transactions/s. All six candidate overload trials complete
correctly within five-second p95, while all six fresh SP01 overload trials time
out. Actual generated input is only 684–689 changed rows/s. Decision: **Keep —
performance**, with resource costs and remaining source, apply, macOS and sustained
qualification constraints retained. SP03a starts from this accepted grouping
runtime and must qualify the bundled SQLite dependency before changing journal mode.


## SP03a contribution ledger

[SP03a's report](sync-performance-sp03a.md) qualifies actual Python SQLite 3.53.1
and Rust SQLite 3.53.2 on both installed platforms. No dependency upgrade is
needed; capture remains DELETE/FULL with unchanged SP02 groups. All 24 fresh
comparison trials, 18 profiler controls and nine component trials pass. Four
contended trials are retained and replaced as whole pairs. Paired median CPU
changes stay below +0.8%, memory within ±3%, and p95 between −1.97% and +0.85%.
Decision: **Keep — reliability/enabling**, with no speedup or equivalence claim.
SP03b can now measure capture WAL separately; its behavior is not qualified here.
