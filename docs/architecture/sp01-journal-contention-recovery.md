# SP01 — Safe journal-read contention recovery

SP01 passes all six mandatory low-load trials and eliminates the four observed
overload resync failures in the **24-trial matched comparison**. All six overload
trials still time out waiting for publication. Six additional low-load follow-up
trials pass, with the original predecessor freshness miss retained.
**Keep — reliability/enabling:** bounded safe recovery works; no throughput
improvement or general latency speedup is established.

[Raw evidence and reproduction](sync-performance-evidence/2026-09-25-sp01/README.md) ·
[Implementation plan](../plans/sync-performance-implementation.md) ·
[Measurement contract](sync-performance-comparisons.md) ·
[Predecessor SP00](sp00-reproducible-comparisons.md) ·
[Issue #88](https://github.com/supabricks/platform/issues/88)

## Recovery boundary

The worker freezes spool identity, bootstrap cursor, after cursor and target once.
Each recognized SQLite BUSY error finalizes statements, closes the connection
and discards its entire partial result. A new read transaction revalidates identity, pruned prefix,
checksums and complete transaction continuity. SQLITE_LOCKED, corruption, history
loss, identity changes and I/O errors do not enter this retry loop.

One monotonic deadline bounds all read attempts to three seconds, or the remaining
worker deadline if shorter. SQLite busy waits are capped at 100 ms and the
remaining budget. Backoff grows from 10 ms to 100 ms; at most 32 read attempts are
allowed. The busy timeout is reset before each statement, and a SQLite progress
handler bounds non-blocking VM work. Backoff time excludes SQLite's busy-handler
wait; total read time includes it. Scheduling/OS stalls can extend wall-clock
return time; the code never grants another budget after the deadline.

The read now precedes initialization because initialization may compact Delta
into a new generation. Exhausted BUSY reads emit a receipt identifying the exact
run, attempt, worker generation, capture identity and cursors, with phase
`journal_before_initialize`. The supervisor rejects a deferral if an apply plan
already exists. Saved plans use the existing checked crash-replay path and never
re-enter journal retry after a partial table commit.

The supervisor persists the same run and its original deadline, waits at least
100 ms before another dispatch, and allows at most three total worker launches
including crashes. Repeated receipt consumption cannot add attempts. At exhaustion
it records `journal_read_busy_exhausted`, fails the publication and parent run,
and preserves the capture prefix. The policy error stops automatic continuous
admission; an explicit retry/resume is needed. This is a bounded failure, not an
unlimited background retry or a claim of successful synchronization.

Continuous pause completes at the safe deferred boundary without another writer.
Cancellation and revoked authority retain the existing conservative fences.
Invalid receipts, storage errors and uncertain mutation still require resync.

## Qualification and measurement design

Focused tests cover actual short and persistent exclusive writer locks, a shorter
worker deadline, recognized extended BUSY codes versus LOCKED/I/O/corruption,
a failure after a partial payload fetch, immutable requests, pruning between
attempts, compaction-before-read prevention, saved-plan replay, subprocess
termination, persistent supervisor attempt budgets, duplicate/stale receipts,
pause, cancellation and authority changes. Published maps and cursors remain
unchanged on deferred/exhausted reads.

The local Rust library suite passes 206 tests (four existing tests ignored).
The final 57-test analytics suite passes in Linux and macOS CI, including
ten focused journal tests. Real-lock
tests synchronize release with observed contention; a virtual-clock test verifies
the exact deadline budget independently of shared-runner scheduling jitter. The benchmark suite
passes 33 tests in the pinned qualifier image. The installed candidate passes
full inventory verification. The Linux comparison uses a diagnostic overlay,
not a signed release; recorded source revisions are operator assertions, while
installed manifest and file hashes identify the exact measured bytes.

The final candidate runtime is frozen at
`b6a0b4b546de460308f6adccfe6aec34528d1893`; the shared harness is frozen at
`c52f466ffc47e141d6decf632f455c2cc4ecf6f2`. The fresh predecessor uses the same
unchanged runtime measured by SP00, source assertion
`92272759ad06af6207b9537a02871f0477e17295`. Both arms use the frozen shared
harness, whose only new measured export fields are bounded operational retry
receipts collected after the workload. Python/native profiling hooks are unchanged.
The journal span moves from inside `apply.plan` to before initialization inside
`apply.run`; predecessor/candidate `apply.plan` totals therefore have different
parents and are not directly comparable. Compare `apply.journal`, unchanged
component spans, and total `apply.run`; do not credit this scope change as a speedup.
Three balanced 4-CPU/50-row/s profiler off/on pairs use the identical SP01 package.
They are separate from the twelve mandatory predecessor/candidate pairs.

The mandatory cells remain 4/16 logical CPUs at 50 offered changed rows/s and
8/16 CPUs at 1,000, three repeats each per arm. Each fresh fixture uses four
clients, two 10,000-row tables, two changed rows per transaction, a five-second
source-only baseline, five-second warmup/catchup, 45-second measured load and
120-second drain. Memory is 16 GiB without swap; complete SMT pairs are pinned on
the same Ryzen 7800X3D/NVMe/ext4 host. Five rolling minutes without active build
observations are required before every trial. All failures remain outcomes;
externally contended pairs are retained and repeated in full.

## Final profiler activation controls

All six accepted controls on `b6a0b4b` completed correctly, met offered load, and
passed five-second p95 freshness, without contention replacements. The same
package and frozen harness were used with profiling off/on in three
alternating-order pairs.

| Same SP01 runtime, 4 CPUs / 50 rows/s | Profiling off | Profiling on |
| --- | --- | --- |
| P95 median [range], ms | 3,792.262 [3,695.531, 4,035.183] | 3,713.210 [3,680.965, 3,732.415] |
| P99 median [range], ms | 4,149.618 [3,913.805, 4,594.104] | 3,930.469 [3,876.237, 3,932.336] |
| Average CPU cores, trial median | 0.759 | 0.791 |
| Changed rows/s, trial median | 50.036 | 50.035 |
| Peak total cgroup memory, trial median | 1,228,840,960 bytes | 1,228,374,016 bytes |

Median CPU increases 4.22% (+0.032 cores). Median p95 differs by −79.052 ms
(−2.08%); paired p95 changes range −7.50% to −0.39%. These screening samples do
not establish a profiler speedup, zero overhead, or statistical equivalence.
Both mandatory comparison arms retain identical profiling; no calibration
percentage is subtracted from their results. Earlier control variants remain
supplementary evidence, excluded from final acceptance.

## Mandatory matrix results

P95 entries are trial median [minimum, maximum], in seconds. Complete means
full-table correctness and transaction attribution; freshness is a separate gate.
All completed low-load trials meet the offered source rate.

| Logical CPUs | Offered rows/s | Predecessor complete / fresh | SP01 complete / fresh | Predecessor p95 | SP01 p95 |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 2/3 | 3/3 · 3/3 | 3.888 [3.773, 5.537] | 3.628 [3.598, 3.667] |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3.637 [3.592, 3.755] | 3.634 [3.614, 3.646] |
| 8 | 1,000 | 0/3 · unavailable | 0/3 · unavailable | unavailable | unavailable |
| 16 | 1,000 | 0/3 · unavailable | 0/3 · unavailable | unavailable | unavailable |

Paired p95 changes at four CPUs are −34.48%, −4.63% and −5.71%; at sixteen they
are −3.75%, +0.26% and +1.18%. Three trials are screening evidence, not proof of a
speedup or latency equivalence. The large four-CPU difference includes an existing
predecessor freshness tail, examined separately below. Historical-baseline deltas
are retained in `comparison.json`; the fresh paired predecessor is the comparator
for attribution.

The median of each cell's **paired** resource changes is:

| Logical CPUs / offered rows/s | Comparable pairs | CPU cores change | Peak total cgroup memory change |
| --- | --- | --- | --- |
| 4 / 50 | 3 | +3.47% | −0.73% |
| 16 / 50 | 3 | +2.64% | −3.51% |
| 8 / 1,000 | 3 | +4.68% | −0.56% |
| 16 / 1,000 | 2 | +4.42% | −0.86% |

CPU compares measured-load windows; memory is the sampled peak total cgroup
usage, including page cache, rather than process RSS. One predecessor overload fails before measured
load, so the sixteen-CPU resource comparison has two pairs. No paired median
CPU/memory regression crosses the plan's 10% investigation trigger; these small
samples do not establish memory savings. Complete per-trial values, medians/ranges,
phase coverage and all paired deltas remain in the machine-readable report.

The final low-load SP01 receipts recover eleven BUSY responses across eleven
reads; all 224 recorded completed reads finish within 180 ms. Overload recovery
and the unchanged bottlenecks are described below.

## Investigated low-load freshness tail

Predecessor four-CPU repeat 2 completes correctly but misses freshness: p95
5,537.057 ms, p99 6,450.074 ms. Successful worker `incremental-3052` spends
1,930.132 ms in `apply.journal`, extending total `apply.run` to 3,300.620 ms,
without an exception. The paired SP01 p95 is 3,627.911 ms. This is retained under
[issue #96](https://github.com/supabricks/platform/issues/96).

Before the mandatory matrix finished, a separate three-pair 4:50 block was
[declared](sync-performance-evidence/2026-09-25-sp01/followup-declaration.json),
using the same frozen packages/harness and seed 20260925. It does not replace any
original outcome. All six follow-up trials pass correctness, input rate and
five-second p95 freshness without contention replacements. Predecessor p95 has
median 3,696.556 ms [3,682.092, 3,697.067]; SP01 has median 3,680.459 ms
[3,661.424, 3,697.076]. Paired changes are −0.56%, +0.00024% and −0.44%.
The follow-up paired median CPU change is 0.00%, and sampled peak memory +0.95%.
These small differences do not establish a speedup or fixed resource overhead.

The [combined analysis](sync-performance-evidence/2026-09-25-sp01/low-load-combined.json)
retains all six four-CPU pairs: both arms complete 6/6; the predecessor is fresh
5/6 and SP01 6/6. Trial p95 median [range] is 3,734.939 ms [3,682.092, 5,537.057]
versus 3,663.992 ms [3,598.248, 3,697.076]. No row-level percentiles are pooled and
no original observation is replaced. The sustained freshness tail remains an
open issue; these short trials do not qualify a five-second guarantee.

## What changed under overload

Across the six accepted overload trials, the predecessor required resync four
times after a profiled three-second `SQLITE_BUSY` read, and reached the drain
timeout twice. SP01 reached the drain timeout in all six, with no failed batch
or resync error. Its whole-fixture receipts record **466 BUSY responses recovered
across 249 completed reads**, 555 completed-read receipts in total, and no
supervisor deferrals. The longest recorded read is 1,390 ms. Counts cover
bootstrap, warmup, measured load and drain; they are not steady-state rates.
Predecessor retry counters are unavailable, not zero. Its three-second exception
boundary and SP01's 100-ms per-attempt boundary also mean raw BUSY counts are not
comparable measures of contention frequency.

Capture still performs exactly four native sync calls per durable COMMIT. Available
45-second load windows show roughly 29.4–30.3 captured transactions/s and
29.7–30.5 ms per COMMIT, consistent with the predecessor. At two changed rows per
transaction, this is about 59–61 captured rows/s under overload, far below the
1,000-row/s target. Source-only baselines produce 846–884 rows/s and load-phase
sources produce 651–658 rows/s where available. At the drain limit, SP01 still has a post-stop lower bound of 8,505–8,694
uncaptured source commits. These are waiting upstream of the published snapshot;
the small captured-spool backlog alone understates total lag. The missing
predecessor load window failed during warmup; it is retained, never filled in or imputed.

Successful apply-worker samples still spend approximately 1.10–1.26 seconds in
directory checks versus 19–21 ms in Delta merge. Their total `apply.run` samples
remain around 1.5–1.7 seconds. These are component observations within failed
pipelines, not complete replication latency or a throughput qualification.

SP02 therefore remains the next slice: group complete capture transactions under
the existing DELETE journal and FULL durability settings, with fresh matched
measurements. Reader/writer coexistence, repeated directory work and source-client
capacity remain separately measured changes in the plan.

## Safety boundaries verified during qualification

A deterministic scheduling regression verifies the deadline crossing *inside* a
retry: after prior BUSY evidence, an internal read deadline becomes a safe deferred
outcome. A deadline without BUSY evidence remains a distinct error. Each attempt
uses the same frozen identity/cursors and original total budget.

A real exclusive writer in the partial-payload fault test also proved that closing
SQLite's connection alone can leave an active cursor/read lease alive while the
exception retains its traceback. The implementation explicitly finalizes every
returned statement before closing the connection, using LIFO cleanup even if a
cleanup operation fails. The writer now acquires the lock during backoff, and the
next consistent snapshot returns each transaction exactly once.

The sustained workload does not force every safety branch. Deliberately persistent
locks and shorter deadlines exercise deferred receipts and bounded exhaustion in
focused Python tests; Rust fault tests exercise persistent supervisor budgets,
unchanged publication/capture cursors, conservative cancellation/revocation fences,
and explicit continuous resume using the same capture identity. Natural workload
retry counts and fault-injection coverage are reported separately.

## Retained superseded experiments and host contention

The first activation-control controller stopped before any trial on EACCES reading
a build descendant's `/proc` I/O. [Issue #98](https://github.com/supabricks/platform/issues/98)
is fixed: unreadable build I/O is unknown and blocks quiet admission on **every**
sample until readable or gone. Unexpected observer errors still stop measurement.
The initial manifest and host samples remain in the archive.

Earlier completed controls remain supplementary: runtime `9098239` has three
accepted pairs plus one contended pair (eight trials); runtime `6ce4439` has three
accepted pairs (six trials). They are excluded from final acceptance because the
candidate changed to fix the deadline/cursor cases above. The first superseded
matrix was interrupted just after its predecessor trial started; the owned
container was removed and absence verified, but there is no completed cleanup
receipt or accepted runtime outcome. The second matrix stopped while waiting
before any trial. Neither contributes success/failure counts to the final matrix.

Two final-runtime pairs were invalidated by external Cargo/test activity and
repeated in full. The mandatory archive contains 28 trials: 24 accepted outcomes
plus four contended outcomes. All 28 teardown receipts have zero leaked/remaining
descendants. Accepted trials have no detected build overlap; their shortest quiet
admission is 304.98 seconds. The 1,500 retained host samples have a maximum gap of
5.08 seconds. The detector observes known builds/descendants; unknown or shorter-
than-sampling activity can escape observation on this shared host.

## macOS freshness qualification

The [earlier debug native-cell run](https://github.com/supabricks/platform/actions/runs/36092406729/job/107937401908)
failed the unchanged five-second assertion: p95 **5,211.412 ms**, p99 6,115.468 ms,
at 49.85 changed rows/s. Worker-start-to-prepared p95 was 2,419 ms, dispatch 49 ms,
and publication 287 ms. Its same-head rerun passed (p95 3,692.607 ms).

The [final installed macOS release check](https://github.com/supabricks/platform/actions/runs/36096958052/job/107962514273)
also missed freshness: p95 **5,728.462 ms**, p99 6,072.373 ms at 49.89 rows/s.
Triggered sync passed; continuous failed its freshness assertion. Worker-start-to-
prepared p95 was 2,859 ms, dispatch 95 ms, and publication 250 ms. Cleanup reported
zero leaked descendants. The installed source `8836184` is GitHub's synthetic
merge of main `00fcf95` and final SP01 `b6a0b4b`, verified through its commit parents.

These unprofiled, shared-runner macOS observations cannot attribute the worker tail
or establish a candidate regression. They remain in
[issue #99](https://github.com/supabricks/platform/issues/99) and the evidence archive;
passing reruns do not erase them or establish a five-second guarantee. The Linux
paired trials are a different host/workload and cannot substitute for macOS
qualification. The freshness threshold is unchanged.

The [same-head installed rerun](https://github.com/supabricks/platform/actions/runs/36096958052/job/107968726253)
passes all installed sync suites, with continuous p95 2,981.593 ms,
p99 3,973.751 ms at 49.79 rows/s. Both attempts remain archived.

## Contribution decision

**Keep — reliability/enabling.** The main comparison, six activation controls,
six follow-up trials and four contended outcomes retain 40 final-runtime trial
receipts; all teardown checks are clean. Earlier variants and interruptions stay
separate. The frozen implementation passes all 47 CI checks after the retained
macOS rerun, plus the local fault/accounting suites described above.

| Slice | Changed mechanism | Measured contribution | Remaining constraint | Decision |
| --- | --- | --- | --- | --- |
| SP01 | Bounded BUSY recovery before any Delta mutation, explicit safe deferral and persistent supervisor budget | Mandatory candidate fixtures recover 477 BUSY responses across 260 reads; observed overload resync failures fall 4 → 0; fault tests verify safe bounded exhaustion | Capture stays near 30 transactions/s, overload still fails 6/6, source input is below target, successful-read and macOS freshness tails remain | Keep — reliability/enabling; no throughput gain |

The accepted predecessor for SP02 is runtime `b6a0b4b`, installed diagnostic
manifest `3a88a7e46e4a222a3757e6960e75da2e230a0f943a0bb793959101041a43da4c`.
The exact package recipe, inventories, all attempts, analysis source and checksums
are in the linked evidence archive. The next slice must compare against this
accepted runtime and the original historical baseline, without crediting these
reliability gains again as new throughput.
