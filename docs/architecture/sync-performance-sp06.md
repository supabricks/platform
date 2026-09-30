# SP06 — Source capacity and a separate load profile

Status: **source qualified; higher-load main complete with an 8-core input
shortfall; profiler controls restarted**, 2026-09-30. SP05 merged as
`d2debe471d749e162ec96a29f2edd3e4ee7e5478`.
The [78-trial source/control review](sync-performance-evidence/2026-09-27-sp06/README.md)
selects eight clients with measured observer cost. The
[higher-load checkpoint](sync-performance-evidence/2026-09-30-sp06-full-stack/README.md)
adds 12 accepted trials: all six 16-core trials deliver about 1,249 rows/s with
p95 below 3.77 seconds, but two accepted 8-core trials supply only 737–839 rows/s.
All pass correctness and cleanup; the input misses remain in the denominator.
The first profiler-control campaign failed on disk exhaustion, with no final
measurement/cleanup receipt. Its evidence is preserved; a fresh 12-trial control
campaign uses the unchanged frozen protocol after disk recovery. No complete
end-to-end qualification or runtime speedup is claimed.
Tracks [#107](https://github.com/supabricks/platform/issues/107),
[#126 (disk exhaustion)](https://github.com/supabricks/platform/issues/126), and
[#127 (durable commit slowdown)](https://github.com/supabricks/platform/issues/127).

This is a measurement-enabling slice. The accepted SP04/SP05 runtime, dependencies,
worker profiler, durability settings and two-row source transaction remain unchanged.
New source-only fixtures have no capture/apply policy. Concurrency is an experimental
workload dimension, never a replication-engine speedup. The historical four-client
matrix remains separate and unchanged.

## Protocol frozen before acceptance runs

Use the immutable accepted SP04 diagnostic package (runtime `e10d515`) and the
existing pinned qualifier image. Source-only and full-stack fixtures run one at a
time with 16 GiB memory, swap disabled, no CPU quota, full SMT sibling groups,
network isolation and verified cleanup. Retain package/harness/image identities,
raw numeric timings, structured profiles, host observations and every attempt.
Require five quiet minutes before each trial; never stop unrelated workloads.
If host contention invalidates a comparison, retain and repeat its entire paired
block. Runtime/correctness/cleanup failures are not performance reruns.

Source-only protocol: two 10,000-row tables, the existing BEGIN / two UPDATEs /
transaction-ID lookup / COMMIT SQL, disjoint keys per client, 60-second warmup
and 300-second measurement. Offer 10,000 changed rows/s to screen saturation;
this is an offered ceiling, not achieved input or a product target. Record exact
acknowledged input inside the 300-second interval and each of its five 60-second
windows. A transaction acknowledged after the interval counts as a tail, not
measured input. Keep tail counts and all numeric timing samples. Check both source
tables against acknowledged generator state after writes stop. Do not enable
Postgres I/O timing, relax fsync, alter transactions or retain a logical slot to
manufacture capacity. Source settings are checked against accepted SP04 settings.

1. Screen 4, 8 and 16 source clients at both 8 and 16 logical CPUs, three repeats
   per cell: 18 source-only runs. Rotate client order within CPU/repeat blocks and
   seed the block order. A contended block repeats all three client variants.
2. Select the smallest tested client count that reaches at least 1,250 changed
   rows/s in **every complete minute of every repeat at both CPU sizes**. If none
   meets preferred headroom, use the same rule at 1,000 and explicitly label the
   missing headroom. If none reaches 1,000, attribute the source limit and keep
   qualification open; do not invent a source-qualified profile.
3. For a provisionally selected count, run source profiler off/on controls at
   8 and 16 CPUs, three fresh pairs per cell: 12 runs. The same in-window and
   per-minute capacity criterion must hold in both arms before final qualification.
   Compare whole-transaction tails, CPU/RSS and throughput; disabled SQL/PG/storage
   observations are unavailable, not zero. Investigate >10% paired median resource
   or latency regressions, smaller repeatable losses and any threshold crossing.
   Source-only profiling adds resource/disk sampling around the existing profiler;
   it does not modify the full-stack profiler or runtime.
4. Measure the 4-CPU envelope with 4 clients and the selected count, three fresh
   paired repeats (six runs; three single runs if 4 is selected). This does not
   gate the 8/16-CPU source qualification. If no count qualifies, retain 4/16-client
   envelope runs and open a scoped SP07 issue instead of claiming success.
5. Run the mandatory unchanged four-client comparison: 12 fresh predecessor and
   12 fresh candidate trials at 4/50, 16/50, 8/1,000 and 16/1,000 CPUs/offered rows/s.
   Both use the same accepted runtime; the predecessor uses the frozen SP04 harness
   and candidate the new harness. Defaults remain five-second baseline/warmup and
   45-second load. This checks measurement continuity, not a runtime gain.
6. Run final full-stack profiler off/on controls, three pairs at 4/50, 8/1,000 and
   16/1,000: 18 trials using the candidate harness and unchanged package.
7. If source qualification succeeds, freeze a separately named profile with the
   selected client count, two 10,000-row tables, two changes/transaction, five-second
   source-only baseline, 60-second warmup, 300-second full-stack load and 1,250
   **offered** changed rows/s. Run three fresh predecessor/candidate pairs at both
   8 and 16 CPUs: 12 runs. Both arms use the identical accepted package and common
   new harness so longer warmup can be represented. Inspect actual input,
   publications, correctness, p95, backlog and drain; the offered rate cannot
   establish either 1,250 or 1,000 achieved replication throughput. Missing source
   headroom and any replication limit remain explicit outcomes.

8. If the separately named full-stack profile completes, run profiler off/on
   controls for that same 1,250-offered/selected-client/300-second profile at
   8 and 16 CPUs, three pairs each (12 additional trials). This qualifies observer
   overhead at the newly supplied rate; the historical four-client controls do
   not establish it. Retain failures and investigate the same regression triggers
   before any conclusion about the new profile.

This additional-profile control requirement was recorded before the source
acceptance screen began; it changes no frozen harness, runtime or earlier phase.

One short source-only functional smoke run is retained separately and excluded
from capacity selection. No fresh runtime package or native build is needed:
source hashes bind the existing accepted/installed-qualified runtime. Freeze the
new harness commit before starting the acceptance series; never edit that checkout.

## Attribution and limits

Retain source COMMIT/transaction tails, active PostgreSQL SyncRep/WAL wait samples,
safekeeper flush histogram/counter deltas, process CPU, cgroup CPU/I/O/pressure,
and host disk busy/queue counters. Host disks are shared observations, not isolated
container utilization. PG timing counters disabled in the accepted settings remain
unavailable evidence; zero counters do not prove zero I/O cost. Resource bookends
include the load-launch delay and final in-flight transaction tail, while capacity
counts acknowledgments strictly inside the declared interval. Memory peak includes
setup/warmup; load samples are separately available with profiling enabled.

Host disk counters can decrease through reset or overflow, as described in the
[Linux I/O statistics documentation](https://docs.kernel.org/admin-guide/iostats.html).
The offline analyzer checks consecutive samples, retains discontinuity receipts,
and marks the affected device's derived utilization/queue/delta unavailable. It
does not guess a wrap modulus or turn a decrease into zero I/O. This was observed
in the first, separately contended block and is tracked in
[#122](https://github.com/supabricks/platform/issues/122). Raw observations and the
frozen measurement harness remain unchanged.

Three repeats are a screen, not statistical certainty. Preserve original failed or
contended outcomes, phase coverage, profiler overhead and sampled-process omissions.
If controls cross the selection threshold or attribution is ambiguous, qualification
is inconclusive until a separately recorded investigation resolves it. No source
runtime fix is bundled into SP06. Any source fix belongs to a separately measured
SP07 slice. Long-duration replication and maintenance claims still require SP11.

## Resumption after temporary artifact loss

The original nine contended trials are no longer available after their temporary
storage disappeared; no accepted source capacity result existed. The
[recovery record](sync-performance-evidence/2026-09-27-sp06-recovery/README.md)
documents the limitation and the new persistent campaign. Restore and verify the
exact historical package inventory and all payload files before running the frozen
harness. Preserve incomplete campaigns explicitly rather than waiting for a
complete screen to archive evidence. This changes evidence retention, not the
measurement protocol or runtime. Tracks #124.
