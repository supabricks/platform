# SP04 — Bounded filesystem work during planning

Status: **merged, measured and qualified; keep for latency improvement** in
[PR #117](https://github.com/supabricks/platform/pull/117), 2026-09-26.
All 84 declared measurements passed correctness; all 60 full-stack trials met
five-second p95 freshness, with no detected contention or rejected trial pairs.
Signed installed Linux/macOS sync qualification also passed.

Planning now makes two full inventories instead of one per Arrow batch. Main
paired median p95 improves 33–40%. This is not a throughput improvement: achieved
overload input falls 2.3–2.8%, and 16-CPU low-load CPU rises 9.47%. Keep the latency
gain with those costs recorded; [#119](https://github.com/supabricks/platform/issues/119)
tracks fixed worker/publication costs. The 1,000-row/s target remains unqualified.

[Raw evidence, reproducible analyses and decision](sync-performance-evidence/2026-09-26-sp04/README.md).
Predecessor: SP03b, merged as `f02dca8`; accepted diagnostic runtime `5382e80`.
Tracks [#91](https://github.com/supabricks/platform/issues/91).

## Change and safety contract

The daemon already admits one incremental writer per installation, stops the
previous worker before retry, and pins both generations against garbage collection.
SP04 adds a nonblocking exclusive flock on the generation directory inode, held
from initialization's return through the worker receipt. No lock file is added.
Initialization/compaction creates a separate temporary generation before this lock;
its existing admission, reservation, durability and path checks remain unchanged.

Only read-only planning reuses accounting. A bounded admission inventory records
all file sizes/identities and directory identities. Every 32-row Arrow batch checks
the deadline, live lease and ancestor identities, directory metadata and actual
free space. A second complete inventory must match before returning the plan.
Namespace changes fail closed between batches; in-place file changes fail before
persisting a plan or allocating Delta output. Same-user arbitrary writes cannot
be prevented by an advisory lock; they are outside cooperative ownership and the
exit reconciliation rejects observed changes. The inventory is never reused for
writes, compaction, recovery, another lease or another planning call. Standalone
planning without a lease retains per-batch full checks. A conflicting writer fails
closed. All existing output reservations, retained-generation checks, post-write
checks, file hashing, fsync and publication checks remain in place.

No change to Arrow batch size, merge settings, worker lifecycle, parallelism,
spool/checkpoint policy, caps, SQL or dependencies. The new directory/file mode
checks reject group/world-writable planning trees.

## Predeclared measurement protocol

Freeze implementation, common profiler and drivers before trials. Package both
arms as immutable unsigned overlays of the accepted SP03b diagnostic package;
retain before/after hashes and checked-hash bytecode proofs. Only the candidate
changes planning/worker code. Signed installed Linux/macOS qualification is separate.

1. Matched component planning: fresh and aged two-table generations, each with
   10,000 rows per table; fresh writes each table once, aged writes 100 rows per
   commit (100 versions). Touch 1 or 1,024 existing keys per table. Three randomized
   pairs per age/key cell (24 measurements). Both arms receive identical file
   trees, transaction payloads, touched keys and selected Delta versions. Compare
   canonical complete plans, scan batches (including empty batches), planning
   wall/CPU time, directory-check time, full traversal and Python stat counts.
   Build fixtures before quiet admission; no fixture writes in measured intervals.
2. Predecessor and candidate profiler off/on controls: three pairs each at 4/50,
   8/1000 and 16/1000 CPUs/offered changed rows per second (36 full-stack trials).
3. Mandatory main comparison: three fresh pairs each at 4/50, 16/50, 8/1000 and
   16/1000 (24 full-stack trials). Four clients, two 10,000-row tables, two changes
   per transaction, five-second baseline/warmup, 45-second measured load, 120-second
   drain, 16 GiB, full SMT siblings, no CPU quota or swap. Retain the same source
   capacity limitation; 1,000 offered is not 1,000 achieved.

Use seeded randomized/counterbalanced ordering, a 300-second quiet interval,
continuous host observations, immutable receipts and complete cleanup checks.
Retain all outcomes; repeat both arms of a contended pair. Functional failures
remain failures, never silently rerun as performance successes. Preserve original
attempts if a candidate correction requires refreezing.

The common private profiler counts Python `stat`, `lstat`, `fstat`, `statvfs`,
`scandir` calls and `Path.rglob` invocations by nested span. These are Python API
calls, not a claim of all kernel metadata operations inside Arrow/Delta/native
libraries. Parent/child counts overlap and must not be summed. Both arms use
identical counting hooks; activation controls quantify their overhead. Attribute
unchanged inventory, retention and durability work outside planning separately.

Acceptance: complete correctness, deadline/disk/unsafe-path and lock/recovery
checks; retained old readers through growth/rotation; reduced matched planning
cost with traversal count independent of batch count. Seek >=80% reduction in
planning directory-check time on aged matched trees, without claiming the same
percentage for complete workers or end-to-end lag. Report fresh-tree regressions
and resource costs. Keep/revise/reject/defer only after the measurements finish.

## Measured contribution

Values below are medians across three fresh pairs per cell. Percentages are the
median of paired percentage changes, not percentages calculated from group medians.
All 24 main trials are correct and fresh. “1,000” is offered input, not achieved.

| CPUs / offered rows/s | Achieved source rows/s, before → after | p95 lag ms, before → after | Paired p95 change | CPU cores, before → after | Paired CPU change | Paired peak RSS change |
| --- | --- | --- | --- | --- | --- | --- |
| 4 / 50 | 50.036 → 50.039 | 3,725.838 → 2,245.806 | −39.72% | 0.886 → 0.788 | −11.17% | −2.57% |
| 8 / 1,000 | 740.623 → 723.469 | 4,508.390 → 3,039.302 | −32.59% | 1.370 → 1.197 | −12.56% | +1.14% |
| 16 / 50 | 50.040 → 50.039 | 3,588.871 → 2,270.216 | −36.37% | 1.383 → 1.514 | +9.47% | +4.48% |
| 16 / 1,000 | 750.648 → 729.691 | 4,528.923 → 3,026.764 | −33.17% | 1.612 → 1.553 | −3.41% | +1.48% |

The matched component screen isolates planning from worker imports and writes.
All 24 measurements have identical paired file trees, Delta versions, scan batches
and complete canonical plans. Candidate planning makes exactly two full walks;
predecessor counts range from 206 to 800. Directory-check medians:

| Tree / touched keys per table | Before ms | After ms | Paired change |
| --- | --- | --- | --- |
| Aged / 1 | 727.856 | 12.584 | −98.27% |
| Aged / 1,024 | 2,779.721 | 28.927 | −98.96% |
| Fresh / 1 | 105.105 | 18.661 | −82.49% |
| Fresh / 1,024 | 93.088 | 19.191 | −79.38% |

The aged-tree diagnostic target is met. Fresh/1,024 is slightly below 80%; neither
that target nor the component percentage describes complete pipeline latency.

## Whole-worker costs and the remaining constraint

These are medians of each trial's median completed successful worker started
within load. Worker lifetimes can cross the load window; imports are outside
`apply.run`. Stage times and parent/child counters overlap.

| CPUs / offered rows/s | Directory checks ms, before → after | Plan ms, before → after | Apply run ms, before → after | Completed workers, before → after |
| --- | --- | --- | --- | --- |
| 4 / 50 | 452.95 → 29.53 | 471.55 → 43.58 | 618.49 → 201.68 | 30 → 46 |
| 8 / 1,000 | 754.88 → 40.89 | 793.54 → 65.17 | 1,025.76 → 307.04 | 24 → 33 |
| 16 / 50 | 500.24 → 27.76 | 518.93 → 41.19 | 659.79 → 202.26 | 29 → 44 |
| 16 / 1,000 | 728.34 → 39.87 | 763.53 → 63.99 | 997.77 → 307.93 | 24 → 32 |

Planning walks fall to two in every main cell; median complete-run walks remain
20, including 18 unchanged walks outside planning. Those cover existing checks
and are not credited as eliminated work.

Faster planning allows more workers under the existing scheduler. Import cost
stays roughly 200 ms per worker, and native apply sync calls remain 26 per worker.
At 16/50, aggregate completed-worker import time rises 5.92 → 9.07 seconds,
native sync calls 754 → 1,144, and lifecycle CPU 52.29 → 56.64 seconds. This
supports the measured cgroup CPU increase (about 0.13 core) despite cheaper
individual plans. It is not an exact CPU decomposition: lifetimes cross measurement
boundaries, and sampled process counters miss short processes and exited tails.
The reason this cost is larger with the wider CPU allocation is not isolated to
a specific library; no parallelism or thread settings changed in SP04.

At 8/1,000 and 16/1,000, achieved source input falls 2.32% and 2.82% paired median.
The 8-CPU source-only baseline is effectively unchanged (868.02 → 868.17 rows/s).
Aggregate native apply sync time rises 3.97 → 5.58 and 4.09 → 5.44 seconds. Capture
prune time rises 0.75 → 0.99 and 0.83 → 0.93 seconds, despite similar prune counts.
This supports shared CPU/I/O competition as a hypothesis for the small input loss;
it does not isolate source wait causality. The latency win therefore comes with
a measured throughput cost in these overload profiles. Neither extra cores nor
lower lag establishes sustained 1,000-row/s replication.

Keep SP04 for the substantial consistent freshness improvement and bounded
planning complexity. Measure maintenance/publication and fixed startup costs
separately under [#119](https://github.com/supabricks/platform/issues/119) before
changing them in SP05/SP08/SP09. Preserve the source-capacity work in
[#107](https://github.com/supabricks/platform/issues/107) for SP06. Do not combine
those mechanisms into this slice or silently weaken durability to recover input.

## Profiler activation controls

All 36 off/on full-stack controls passed correctness and freshness, with no
rejected/contended pair. Paired median activation changes:

| Runtime / CPUs / offered rows/s | p95 | CPU | Peak RSS | Achieved source |
| --- | --- | --- | --- | --- |
| Predecessor / 4 / 50 | +0.22% | +5.77% | +1.62% | −0.002% |
| Predecessor / 8 / 1,000 | −0.19% | +6.02% | +3.98% | −1.16% |
| Predecessor / 16 / 1,000 | +0.64% | +3.69% | −1.65% | −0.71% |
| Candidate / 4 / 50 | −1.84% | +5.21% | +1.36% | −0.014% |
| Candidate / 8 / 1,000 | −0.80% | +3.61% | +1.05% | −0.61% |
| Candidate / 16 / 1,000 | +0.08% | +1.59% | −2.55% | −1.32% |

No median activation regression exceeds the 10% investigation trigger. Observer
CPU cost is still real and differs by arm. These diagnostic package measurements
are a three-repeat screen, not statistical certainty or signed-release capacity
certification. No 16/50 control was predeclared; do not infer zero overhead there.

The frozen generic comparison's `apply_directory_ms` includes only the old
`apply.boundary` span. Its original receipts are retained without rewriting.
`planning_analysis.py` supplies the correct SP04 total: `apply.boundary` plus
`apply.planning_inventory` plus `apply.planning_check`, which do not nest in each
other. Use that total for the directory tables above. Disabled profile counters
are unavailable, not evidence of zero cost.

## Qualification, identities and recovery

Frozen runtime/common harness: `e10d5152f5681af7c233701e0fd75c100cbf4c6c`.
Both immutable unsigned overlays use accepted SP03b native runtime/dependencies
`5382e80032bb4fc70706f112db26f8415849a961`; both get the same filesystem profiler.
Package receipts bind every changed source/checked-hash bytecode file. Candidate
production sources equal the diagnostic worker after removing only the profiler
import/install hook; the planning module is byte-identical.

The full analytics suite has 93 tests. Eight new planning tests exercise bounded
scan count and conservative fallback, deadlines/free space/file and byte caps,
namespace mutations/symlinks, in-place growth/same-size edits, root/ancestor
replacement, unsafe modes, invalidated leases, overlapping processes and SIGKILL
lock release. Existing incremental/maintenance tests retain replay, reservation,
compaction and old-reader coverage. Forty performance-harness tests and three
installed-evidence collector tests pass; the collector rejects a missing or
mismatched planning module.

The first CI attempt exposed a subprocess test that assumed the analytics working
directory ([#118](https://github.com/supabricks/platform/issues/118)). Its original
failure is retained; commit `7afbe35` fixes the test working directory without
changing runtime code. Commit `cde8839` adds the planning module to installed
source inventory. Production worker/native inputs remain identical to the frozen
runtime. The complete [qualified native-release run](https://github.com/supabricks/platform/actions/runs/36249393933)
passed, including signed installed triggered, continuous, maintenance and governed
sync suites on both platforms and the twelve WAL fault checks. Qualification uses
workflow head `cde8839c21dd37286ce2d9979a6a133f89ae31d8`; GitHub checked out merge
commit `b12db00554c66796f6881bb02e00fa5a023caca8`, whose complete tree is identical.
Raw reports, validated source/archive identities and exact job links are retained.

No format migration or persistent cache is introduced. Reverting the planning
change restores conservative full per-batch checks without transforming data.
The kernel releases the lease on process exit; retries rebuild all accounting.
Process crash tests do not establish physical power-loss behavior. Short trials
do not qualify days of retention growth; long-duration qualification remains in
later slices. The SP03b retry outcome is retained separately and does not count
as an SP04 trial.
