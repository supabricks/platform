# SP11 memory-screen correction — issue #168

The first 8-CPU/900-second fixture in launch 02 failed policy v1. Its raw
result, timing stream, resource samples, windows and cleanup/receipt hashes are
preserved here with `failed-` prefixes. It achieved 1,249.860 changed rows/s,
3,362.706 ms overall p95, 3,624.184 ms worst-window p95 and 1.925 s drain. Data
equality, reopen and cleanup passed; no host contention was observed. Early/late
median total cgroup memory was 1,706,717,184 / 2,531,547,136 bytes. This remains a
failed attempt, with zero of eight steady fixtures accepted.

Sampled aggregate runtime RSS was roughly flat (early/late medians 1,986.21 /
1,972.24 MB). Summed RSS double-counts shared pages and cannot be subtracted from
cgroup physical memory. Original evidence did not record `memory.stat`, so it
cannot retrospectively establish the exact anonymous/cache split.

## Logical correction and validation

The harness retained every source transaction and capture marker as Python
objects. `storage-probe.py` reproduces 600,000 transactions in separate processes:
original storage added 270,300 KiB peak RSS; packed storage added 25,136 KiB
(90.7% less). This is observer-storage evidence, not a runtime speedup.

Source timing storage now uses exact packed records; sparse xid pages preserve
out-of-order commits, gaps, 32-bit boundary values and missing-marker failures.
Budgets fail closed. The runtime package and workload SQL are unchanged.
One-second memory attribution records qualifier RSS and raw cgroup counters.

Policy v2 explicitly changes the memory metric, retaining the 1.25x +256 MiB
formula: total cgroup memory minus clean inactive file cache, conservatively
charging dirty/writeback bytes. Qualifier memory is not subtracted. Growth in
anonymous/kernel/active-file memory still fails; any measurement-interval high,
max, OOM or OOM-kill event also fails. Raw cgroup growth retains a separately
reported v1 screen. Missing attribution cannot qualify. The frozen configuration
and each trial's policy must agree. This corrects the conflation of reclaimable
cache and allocation growth; it does not prove disk retention is bounded.

A five-minute installed diagnostic uses the original storage plus the attached
attribution-only patch against `f6c030b`. It is a plumbing/attribution screen,
not a capacity qualification. The corrected installed screen and full fresh
campaign must be recorded separately. No original result will be reclassified.

At implementation time all 100 performance harness tests passed, including
lossless attribution, sparse/wrapped xids, exhausted budgets, dirty/writeback
cache, memory-pressure failure and missing attribution. The fresh campaign
retains six 15-minute and two 60-minute fixtures; other SP11 phases remain pending.

The original-storage diagnostic completed with exact table equality and zero
remaining descendants. First/last 60-second medians during measured input show
qualifier RSS 111.99 -> 205.46 MB, total cgroup 1503.89 -> 1856.79 MB, and
inactive file cache 345.13 -> 633.41 MB. These counters demonstrate both
observer allocation and cache growth; they do not retrospectively partition the
original failed 15-minute run. See `diagnostic-memory.json` and raw compressed
resource samples.

## Corrected installed screen and launch 03

The corrected 300-second screen passed all individual gates at **1,246.285
changed rows/s / 3,073.364 ms p95**, with exact two-table equality, journal
reopen, foreign-key checks and zero leaked/remaining descendants. It is still
`screen_only`: one five-minute interval does not qualify sustained growth.
The frozen analyzer independently reproduces its windows exactly.

First/last 60-second qualifier RSS is 86.99 -> 103.76 MB (original-storage
diagnostic: 111.99 -> 205.46 MB). Working memory is 1138.06 -> 1121.41 MB;
raw cgroup memory and cache counters remain in the archived resource samples.
The two sequential short diagnostics establish instrumentation behavior, not
a statistically qualified runtime performance improvement.

**Launch 03 is active**, using frozen harness `c05a70a1440482f1525f0f0ab9ca3b0512204c97`,
policy v2 and the unchanged #157 runtime. The normalized config and original
config hash are archived here. The supervised eight-fixture campaign writes
live status to `build/issue168-20261006/steady-03/status.json`. It stops on failed
gates or contention and does not replace prior attempts. Qualification and
issue #168 sustained confirmation remain pending. Other SP11 phases are unchanged.
