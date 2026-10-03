# SP10a — Capture journal backend contract

Status: measured and accepted, 2026-10-02 UTC. Keep the backend contract; no
performance improvement claimed. SQLite remains the only backend;
writer and reader processes retain their existing direct database access.

## Boundary

`capture/journal.py` defines owner and snapshot operations: bounded atomic append
and metadata establishment, durable metadata/cursor access, replay anchors,
streaming verification/range reads, bounded prune selection/commit, physical
accounting and close. The SQLite implementation is in `capture/sqlite_journal.py`.
The contract keeps existing fixed-width hexadecimal keys, payload bytes, hashes,
metadata encoding and persisted schema. Prune tokens are private to the owner.
There is no backend registry, IPC transport or selectable new engine.

`Spool` retains identity checks, contiguous ordering, replay/hash validation,
barrier parsing, publication-authorized prune boundaries and feedback cursor
validation. Incremental storage retains frozen identity/bootstrap/target checks,
history validation, checksums and its 16 MiB materialization bound. The backend
never grants replication feedback or publication authority.

SQLite owns SQL transactions, WAL/FULL settings, path/sidecar checks, the exclusive
writer lock, shared reader leases, physical reservations and checkpoint/vacuum
policy. The schema, SQLite dependency, 128-record / 4 MiB append-group maximum,
32-value / 2 MiB-per-value snapshot metadata limit, 4 MiB payload bound, 256-row /
16 MiB prune selection, checkpoint settings and reclamation thresholds remain.
A snapshot finalizes cursors before closing its connection and lease, including
when a retained exception traceback would otherwise keep a read lock alive.

Ambiguous COMMIT responses reopen the backend and raise `CommitUncertain`;
`Spool` revalidates identity, the complete retained chain, cursor and proposed
records before accepting anything. Only the same four previously recognized
SQLite BUSY codes become the backend-neutral `ReadBusy` outcome. The unchanged
apply policy owns the three-second/32-attempt retry budget; LOCKED, I/O and
corruption failures remain fatal, and no retry crosses partial Delta application.

## Validation and measurement contract

The initial extraction passes all 100 existing analytics tests. Four new contract
tests exercise raw legacy SQLite format/replay in both directions, pinned
snapshots across concurrent append/prune, rejection before backend mutation and
cursor/lease release while an exception traceback remains alive. Existing tests
retain crash, ambiguous commit, corruption, disk pressure, bounded-reader,
checkpoint and authority checks; SQLite-specific fault injection now addresses
the implementation directly.

Use the accepted SP09a `candidate-runtime-02` package as predecessor. Candidate
overlays only capture policy/backend/contract, capture worker status calls and
incremental journal reads, with checked-hash bytecode. The native executable,
SQLite/Python dependencies, profiler, batching, checkpoints and SP09a worker reuse
are identical. Verify every package manifest and exact diagnostic capability
before admitting a trial. Source and installed smoke results precede the campaign.

Freeze both package identities, source revision, campaign/controller, unchanged
SP06 workload harness, image and configuration. Run sequentially with whole SMT
affinity, 16 GiB, no swap/CPU quota, 64 GiB free-space admission, five minutes of
build quiet, and at most two whole-pair contention replacements. Keep original
invalid attempts; source/runtime failures are not contention replacements.

The campaign comprises:

- Three fresh predecessor/candidate durable journal component pairs, exercising
  128 groups of 32 × 1 KiB records, at least three real prunes, a pinned reader,
  physical-byte accounting, reopen and replay. These opaque-payload rates are
  component measurements, not PostgreSQL-to-Delta capacity.
- Three full-stack lifecycle pairs, retaining same-process epochs, worker kill,
  idle retirement, pause/restart/schema fences and atomic/pinned epochs.
- Three observer-off/on pairs at both 8/16 CPUs on the predecessor, before main
  comparisons, and the same controls on the final candidate (24 control trials).
  Both modes use identical source load and independently compare both published
  table versions from one epoch with the frozen PostgreSQL source. They report
  source rate, whole-stack CPU and bounded final drain; they cannot qualify p95
  freshness without the transaction observer. Profiling stays off in these controls.
- 24 historical main trials: three matched pairs at 4/16 CPUs and 50 rows/s,
  8/16 CPUs and 1,000 offered rows/s; four clients, five-second warmup and
  45-second measurement. Preserve source-limited outcomes explicitly.
- 12 qualified main trials: three matched pairs at 8/16 CPUs, eight clients,
  1,250 offered rows/s, 60-second warmup and 300-second measurement.
- 36 candidate profiler activation controls over those same cells. The profiler
  itself is unchanged, so no new profiler instrumentation bridge is introduced.

This is 96 performance/control trials plus 12 component/lifecycle fixtures.
Run under user systemd with a ten-second heartbeat. Check service liveness and
heartbeat age as well as status. Errors stop for investigation; no automatic
runtime retries or silent reset of evidence. Completion means review is required,
not automatic acceptance or merge.

Treat this slice as an abstraction-cost check. Require correctness, durability,
cleanup and qualified input/freshness to pass. Review every cell and raw receipt;
flag paired median source reduction >5%, p95 increase >5%, or CPU/peak memory
increase >10% for investigation before accepting. These are review screens, not
statistical equivalence claims. Preserve lower-level costs and all individual
results even below those thresholds. No stage-percentile arithmetic or observer
cost subtraction is used to manufacture a runtime benefit.

SP10b will measure single-owner IPC separately with SQLite. SP10c will compare
engines through that identical interface/ownership and against direct SQLite,
including the required 30-minute engine comparison and repeated maintenance.
SP11 retains full sustained-capacity/rotation/reader-pressure qualification.

## Installed screening checkpoint

Runtime/controller source `37a5233` and the immutable candidate package pass the
installed durable journal and full lifecycle screens, including same-process
reuse and kill recovery. Five-second observer-off/on smoke runs each converge to
both frozen source tables. All four fixtures report successful cleanup and zero
leaked/remaining descendants. These are functional screens, not quiet-host
performance qualification. [Receipts and package proofs](sync-performance-evidence/2026-10-02-sp10a-screen/README.md)
are retained. The native binary and profiler match the accepted SP09a package.
The supervised measured campaign uses new `campaign-01`, frozen `harness-01` and
`config-01.json`; the final review is below.


## Final measured review

All 96 performance/control trials and 12 component/lifecycle fixtures completed,
with no contention replacements or failed trials. Receipt hashes and metrics were
recomputed using the frozen controller. Every fixture cleaned up with zero leaked
or remaining descendants. [Reviewed evidence, individual results and reconstruction
script](sync-performance-evidence/2026-10-02-sp10a-reviewed/README.md) are retained.

| CPUs / offered rows/s | Actual source, predecessor → candidate | p95 lag, predecessor → candidate | Paired p95 change | Paired CPU change | Paired peak-memory change |
| --- | --- | --- | --- | --- | --- |
| 4 / 50 | 50.039 → 50.037 | 1.921 → 1.907 s | +0.33% | +0.24% | +1.01% |
| 16 / 50 | 50.038 → 50.040 | 1.891 → 1.902 s | +1.00% | +0.23% | +3.68% |
| 8 / 1,000 | 739.944 → 739.149 | 2.259 → 2.226 s | −1.16% | +0.13% | −1.26% |
| 16 / 1,000 | 753.494 → 755.248 | 2.242 → 2.253 s | +0.50% | −0.41% | +2.38% |
| 8 / 1,250 qualified | 1,249.542 → 1,249.744 | 3.000 → 3.020 s | +0.53% | +0.40% | +0.71% |
| 16 / 1,250 qualified | 1,249.742 → 1,249.896 | 3.011 → 3.030 s | −0.25% | +0.41% | −1.56% |

Values are medians of three fresh trials; changes are medians of matched changes,
so they need not equal changes between the displayed medians. Every main trial
passes correctness and five-second p95. All 12 qualified trials meet input;
the historical four-client overload remains source-limited and does not qualify
1,000 rows/s. No main cell crosses the predeclared regression screens. Three
repeats do not prove statistical equivalence or sustained capacity.

The direct journal component delivers 4,319 → 4,309 transactions/s median
(−0.25% paired); component CPU rises 2.54% paired and physical peak stays
13,772,856 bytes. Each fixture performs eight prunes, preserves its pinned snapshot
and passes reopen/replay. These opaque 1 KiB records are not pipeline throughput.
Lifecycle idle CPU changes 0.0853 → 0.0855 cores. API p95 medians are
38.398 → 38.668 ms, with +8.44% paired change across noisy individual values
(−11.18% to +12.81%); this is an auxiliary API metric, not the declared freshness
screen. All worker reuse, kill, pause/restart, schema and atomicity checks pass.

Profiler off/on controls on the same candidate show +7.90–10.59% paired CPU,
including the >10% screen at 8 CPUs / 1,000 offered. Low-load p95 rises 4.59% at
4 CPUs and 6.64% at 16 CPUs (the latter crosses the 5% screen). These are
instrumentation activation effects: both main arms use the identical profiler,
and the off/on control changes no runtime code. Qualified p95 changes −0.19% /
+0.23% at 8/16 CPUs, with +8.00% / +9.00% CPU and +5.39% / +6.47% peak memory.
Record this measurement limitation rather than subtracting profiler costs or
claiming an abstraction regression from the activation comparison.

Observer off/on controls add 1.40% / 1.69% CPU on the predecessor and 1.84% /
1.46% on the candidate at 8/16 CPUs. All 24 controls sustain approximately
1,250 rows/s and independently converge to both frozen source tables. Final-drain
medians span 1.66–2.66 seconds; timing depends on the final epoch phase and is not
p95 freshness. No observer-cost subtraction is used.

Decision: retain the contract and proceed to SP10b's separately measured private
owner transport. No engine, storage-format, durability or concurrency change is
accepted by this review. SP10c and SP11 gates remain outstanding.
