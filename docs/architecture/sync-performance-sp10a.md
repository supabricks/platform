# SP10a — Capture journal backend contract

Status: implementation and qualification in progress, 2026-10-02 UTC. No measured
performance improvement or neutrality claim yet. SQLite remains the only backend;
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
`config-01.json`; its final review remains pending.
