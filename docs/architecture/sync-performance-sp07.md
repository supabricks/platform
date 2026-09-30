# SP07 — Attribute the source commit limit before selecting a fix

Status: **diagnostic implementation and frozen experiment protocol**, 2026-09-30.
Tracks [#127](https://github.com/supabricks/platform/issues/127). Based on the
completed [SP06 measurements](sync-performance-sp06.md), on a branch stacked over
SP06's unmerged PR #121. No source/runtime change or performance improvement is
claimed. Earlier accepted shortfalls remain in their original denominator.

## Retained evidence and exact source path

The [recomputed attribution](sync-performance-evidence/2026-09-30-sp07-attribution/retained-analysis.json)
covers every profiled higher-load main attempt, including excluded pairs, and
all profiled final controls. Archive checksums are verified before analysis.
During the accepted slow 8-core pair, source COMMIT p50 rises from about 11 ms to
17.042/19.771 ms, capture COMMIT mean rises from about 7.5–7.8 ms to
12.053/13.783 ms, and safekeeper flush mean rises from about 4.2 ms to
6.39/7.37 ms. Safekeeper flush completions fall from about 204/s to 132/115/s.
Source throughput falls to 839/737 rows/s while total CPU use remains near one
core. These are coincident waits across durable writers, not isolated hardware
latency or proof of a specific competing process.

Runtime component pins in `components/components.lock.json` are PostgreSQL
`56692dfb680281a963c7470fc7f0fec7f65ecfd4` and Neon
`1c6fa095261112aae239beef5a221b484703d49a`. The audit uses those commits, not the
current sibling checkouts (which have moved).

- PostgreSQL `src/backend/access/transam/xact.c` calls `XLogFlush` before
  `SyncRepWaitForLSN`. Local WAL durability and remote acknowledgement are distinct
  obligations; their presence is not evidence of redundant flushes.
- `xlog.c` already coalesces concurrent flush requests under `WALWriteLock` and
  rechecks whether another backend satisfied the requested LSN.
- Neon's `pgxn/neon/walproposer_pg.c` reports the safekeeper quorum's durable
  position as standby write/flush progress, allowing synchronous waiters to wake.
- `safekeeper/src/wal_storage.rs` uses `sync_data` for WAL flushes (with
  `sync_all` for appropriate metadata operations). `metrics.rs` measures elapsed
  async completion, including scheduling. `receive_wal.rs` and `safekeeper.rs`
  retain their existing write/flush/ack ordering. No proven redundant flush or
  missing group-commit mechanism has been identified by this audit.

Sampled PostgreSQL active waits concentrate in SyncRep and WALWrite/WalSync.
Wait-sample fractions are not exact transaction time. PG WAL timing is disabled
in the accepted runtime; zero timing counters are unavailable measurements.
Historical host disk counters include discontinuities (#122), and the build
monitor does not attribute I/O from non-build processes. Neither is a basis for
retroactively declaring the slow pair contended.

## First experiment: qualify additional host I/O attribution

Before any runtime intervention, run **three fresh 8-core pairs (six trials)**.
The only arm difference is a read-only host process I/O sampler: predecessor off,
candidate on. Both arms keep the existing worker/PG/safekeeper profiler enabled
and use the same SP04 runtime package and frozen SP06 workload harness `be4701c`.
This is a diagnostic observer control, not a runtime performance comparison.

Freeze before launch:

- Eight clients, two 10,000-row tables, two changed rows per source transaction;
  1,250 offered changed rows/s; five-second baseline, 60-second warmup and
  300-second full-stack measurement. Same fsync, replication and SQL semantics.
- Eight logical CPUs containing whole SMT sibling groups; 16 GiB, no swap or CPU
  quota, no container network. Existing cleanup/correctness/freshness accounting.
- Three alternating-order randomized pairs, seed 20260924. Five-minute build-quiet
  admission; retain and repeat whole contended pairs, at most three attempts per
  pair. Measurement/cleanup failures stop the campaign, without performance retries.
- At least 64 GiB free before each trial. This admission check is not a continuous
  low-disk guard; remaining failure-state/disk work stays in #126.
- One-second host samples, maximum 64 MiB of raw sample JSON per trial. Numeric
  PID/start-time identities, CPU and physical read/write counters, parent PID,
  fixed role labels and opaque cgroup hashes only. Never export argv, file paths,
  SQL, row values or credentials. Bind the owned Docker container to its cgroup
  hash to distinguish fixture I/O from readable external processes.
- Record controller-process CPU seconds and sampled RSS in **both** arms, outside
  the fixture cgroup. Keep lifetime peak RSS labeled separately because it can
  include previous trials. This exposes observer overhead outside the 16-GiB
  stack limit; container CPU alone cannot measure that overhead.
- Preserve permission denials, exits, new identities, counter resets, sampler
  wall time and missing coverage. Root-owned processes may be unreadable. Do not
  interpret inaccessible or short-lived processes as idle. The existing build
  monitor still controls acceptance; new observations cannot selectively erase
  slow outcomes. The sampler changes no process priority, affinity or host setting.

Review input, correctness, p95/p99, CPU and memory for every pair. Investigate
>10% median resource/latency cost, smaller repeatable costs, or any pass/fail
transition before accepting the new observer. Keep raw overhead; do not normalize
it away. Compare commit/flush timing with I/O from the owned group and visible
external groups. Co-movement is a diagnostic lead, not causal attribution.

## Decision after the diagnostic

If a slow interval recurs with sufficient coverage, use it to choose one narrowly
scoped causal experiment. Any source fix requires its own immutable package,
crash/recovery checks, source-only comparison, mandatory unchanged full-stack
matrix and qualified-profile repetitions/activation controls before acceptance.
No larger transactions, weaker durability or additional client count is part of
this slice.

If the shortfall does not recur or coverage cannot distinguish causes, retain an
inconclusive result and #127. Source capacity already meets the target in SP06;
that supports declining speculative source changes, not declaring the historical
stall fixed. Sustained behavior and release claims remain SP11/SP12 work.

## Functional preflight and protocol revision

The initial short on/off functional check passed with exact correctness and
cleanup. The sampler bound its container cgroup and read all sampled owned-process
I/O counters; inaccessible unrelated processes remain explicit. Before any
full-duration trial began, admission was stopped to add controller-process CPU/RSS
accounting in both arms. The waiting manifest and zero-measurement hold are
archived unchanged. A separately frozen controller runs another functional check
then `observer-controls-02`; no performance result or retry budget was reset.
