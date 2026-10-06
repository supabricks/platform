# SP11 — sustained correctness, recovery and scaling

Status: six 15-minute fixtures passed after #168; the first one-hour fixture
stopped on sustained slowdown [#169](https://github.com/supabricks/platform/issues/169).
The catalog generation-index correction passes installed validation, but launch 04
stopped after its first one-hour trial: 800.951 rows/s fails throughput; worst-window
p95 4.817 seconds passes freshness. Correctness, memory and cleanup pass.
Source commit/storage diagnosis is in progress,
October 6, 2026. SQLite is the
selected implementation from the [SP10c review](sync-performance-evidence/2026-10-05-sp10c-review/README.md).
This does not waive SP10c's missing experimental sustained arms. SP11 measures
the selected SQLite path; it does not adopt RocksDB or expand the supported SLO.

## Execution sequence and scope

1. **Steady baseline (implemented; rerun after #169):** start with the fresh
   8-CPU 60-minute fixture that previously failed, then three fresh 15-minute
   fixtures at each of 8/16 allowed logical CPUs, then the 16-CPU 60-minute fixture.
   Alternate CPU profiles between short repeats. Offer 1,250
   changed rows/s with eight clients, 60 seconds warmup, 16 GiB cgroup memory and
   no swap. Use an immutable overlay of the previously qualified #157 SQLite-owner package,
   changing only the native binary and catalog version for the #169 generation
   lookup index. Verify every shared payload file. Its identity remains separate
   from the harness and merged source. This is local
   Linux performance evidence, not an exact newly assembled release qualification.
2. **Maintenance and readers (pending):** at least three observed successful
   checkpoint/reclamation cycles and an actual generation rotation. Exercise
   pin/unpin, Sail epochs held over rotation/GC, retained roots and post-unpin
   cleanup. A prune call count is not proof of a completed maintenance cycle.
3. **Capacity and catch-up (pending):** three repeats at 4/8/16 CPUs around
   50/250/500/1,000/1,500 rows/s, source-only ceilings, bursts, and at least
   30 seconds of apply pause with capture/input continuing. Preserve overloads.
4. **Workload and interference (pending):** wider/larger data, hot keys, inserts,
   deletes, primary-key changes, large supported transactions, many tables, idle
   sources, triggered barriers and concurrent OLTP/SQL/notebook/catalog readers.
5. **Fault qualification (pending):** group commit/feedback, journal read,
   table/descriptor/publication, pruning/checkpoint and relevant migration
   boundaries; pressure/ENOSPC, corrupt history, lost WAL, source restart and
   revocation. Verify prior coherent epochs and no missing acknowledged data.

A passing first campaign is only a steady-baseline result. It cannot complete
SP11, replace those other gates, establish EC2 scaling or qualify TPC-DS. Runtime
fixes found here require a separately measured slice before a fresh affected run.

## Predeclared steady analysis

`sp11_trial.py` reuses the existing workload, observer polling, shutdown, resource
sampler and retained-chain verifier. After #168, source timings use packed 28-byte
records (128 MiB budget) and markers use sparse xid pages (64 MiB per map).
Missing markers, conflicting reused xids, unexpected timing fields and exhausted
budgets invalidate a run; no transactions are dropped or approximated. Profiling
remains off. After the load/drain interval,
it exports a gzip stream of fixed 16-byte network-order records: two doubles for
COMMIT acknowledgement and first observed covering publication, in milliseconds.
No row payloads, SQL or credentials are recorded. The stream has a 128 MiB
uncompressed budget. This lets review independently recompute window results.
The existing durable-marker budget remains 256 MiB (about 64 MiB for a one-hour
1,250-row/s run plus warmup), without changing durability or product limits.

Evaluate 300-second windows every 60 seconds, plus a final aligned window. Each
window must commit **and publish** at least 1,000 changed rows/s. Publication
counts use actual wall-clock publication time, excluding a later drain; only
transactions originating in this measured source interval count. Lag uses each
window's COMMIT cohort, including its bounded subsequent publication. Require
p95 <=5,000 ms in every window and report p99/max and the worst window. This is
not a claim about every mathematically possible rolling interval.

Compare first/last five-minute observations. Before running, freeze these
investigation thresholds in `sp11_analysis.POLICY` and the campaign configuration:

- Late backlog p95 <=1.25 × early p95 +1 MiB; fitted backlog slope <=1 MiB divided
  by measured duration. Report full series and maxima, not just this screen.
- **Memory policy v2 (#168):** late median working memory <=1.25 × early median
  +256 MiB. Working memory is cgroup `memory.current` minus
  `max(0, inactive_file - file_dirty - file_writeback)`. This discounts only clean
  inactive file cache; qualifier allocations, anonymous/kernel memory, active
  file cache and dirty/writeback bytes remain charged. Require zero increases in
  cgroup memory `high`, `max`, `oom` and `oom_kill` events during measurement.
  Export one-second raw cgroup memory/stat and qualifier RSS alongside runtime
  process RSS. Raw total-memory growth keeps its original formula as a diagnostic
  screen, with its pass/fail result visible separately from qualification gates.
  The original v1 failed result is retained and is **not** reclassified. The policy
  change is explicit: inactive cache grows as retained files are written, even
  when runtime allocations stay flat. It cannot establish bounded disk retention;
  that remains part of the pending maintenance/GC phase.
- Sampled capture spool <=512 MiB; no resource inspection errors or sample gap
  exceeding five seconds (including interval edges). Sampling is not a hard quota
  and capture spool size is not whole-stack storage. Physical high-water,
  retained generations and pinned-reader behavior require the later phases.
- Existing complete table equality, source-rate accounting, whole-run freshness,
  120-second maximum drain, foreign-key checks, journal reopen and zero leaked or
  remaining owned descendants must pass. A mean cannot hide a failed repeat.

## Supervision and evidence

`sp11_campaign.py` validates frozen harness, runtime manifest/binary, qualifier
image, host topology and free disk. One host monitor carries quiet credit across
trials; only real competing activity or a sampling gap resets it. It emits
10-second heartbeats while waiting/running, archives hashes/cleanup/contention
for every attempt and stops on the first failed or contaminated fixture. It does
not restart or silently replace measurements. SIGTERM stops its owned container even if the Docker client has already exited
([#166](https://github.com/supabricks/platform/issues/166)); the service uses
`KillMode=mixed` so the controller can finish this cleanup. Launch 01 was stopped
during quiet admission before any fixture began. Its frozen inputs/status remain
retained; launch 02 uses the corrected supervisor. Launch 02 stopped after its
first 15-minute fixture failed memory policy v1. Its original sources, thresholds
and result remain preserved. Launch 03 used compact observers and memory policy v2 with the unchanged runtime:
six short fixtures passed, but the first one-hour fixture failed throughput and
freshness. Its memory/cleanup/correctness checks passed. The #169 rerun freezes
the indexed runtime in a new campaign, preserving the original failures and gates.

Eight steady fixtures contain **210 minutes of measured load**, plus eight warmups,
startup/cleanup and one initial quiet admission (roughly four hours without
contention). A stopped campaign is evidence requiring investigation, not a pass.
A ten-second installed screen checks plumbing and teardown only; its gates never
qualify throughput or SP11. Live status is stored under
`build/issue169-20261006/steady-04/status.json`; see the [#169 evidence](sync-performance-evidence/2026-10-06-issue169/README.md); the [original launch record](sync-performance-evidence/2026-10-05-sp11-start/README.md)
and [#168 correction evidence](sync-performance-evidence/2026-10-06-issue168/README.md)
identify each attempt separately.
