# SP10c — controlled RocksDB comparison

Status: implementation and qualification in progress. SP10b merged in
[#145](https://github.com/supabricks/platform/pull/145), main commit `3a35d1a`.
No RocksDB adoption or migration is authorized by this experiment. Production
continues using SQLite; the experimental backend lives in the performance harness
and is selected only by a hashed, disposable installed-package overlay.

## Implementation and dependency findings

`rocks_journal.py` implements the existing capture owner contract with unchanged
payloads, LSN ordering, hashes, group thresholds, replay checks and pruning policy.
Each append or prune stores its records and metadata together in a raw-mode
WriteBatch with `sync=True` and WAL enabled. Native write errors stop capture;
there is no in-process retry or acknowledgement from an uncertain result. Restart
runs the existing identity and complete retained-chain verification. The RocksDB opener uses a persisted
format marker and rejects any SQLite file. Experimental data directories are
disposable fixtures, never inputs to a production SQLite installation.
This is not a migration mechanism.

The separately locked experiment uses `rocksdict==0.3.29`, with Linux x86-64 and
macOS arm64 wheel hashes. It introduces no product dependency. Native compilation
is not required for these wheels; package assembly time and installed-byte delta
are measured separately from unmeasured upstream source-build time.

The published binding has a reproduced snapshot iterator defect:
[#149](https://github.com/supabricks/platform/issues/149). Snapshot point reads see
old data while snapshot iteration sees newer data. The experiment instead keeps
one ordinary raw iterator for both metadata and record seeks. RocksDB's iterator
provides one consistent implicit snapshot; it is released before IPC serialization
and checked for native iteration errors. Contract tests cover append/deletion,
flush, delayed first seek, cancellation and concurrent readers/writers.
[Binding source](https://github.com/rocksdict/RocksDict/blob/e7205d989a2ff74da44e9d2189b9a72663a00324/src/snapshot.rs),
[RocksDB iterator contract](https://github.com/facebook/rocksdb/wiki/Iterator).

Memory configuration bounds memtable targets, block cache, file handles and
background jobs. Physical accounting includes every database file, including WAL,
SST, manifests, options and logs. Conservative logical/physical reservations apply
backpressure before writes. These are **not a filesystem hard quota or an RSS
proof**: native compaction and pinned iterators can retain temporary files/buffers.
Sustained high-water measurements and pressure tests remain required. This
limitation prevents a release-readiness claim even if short throughput tests pass.

## Measurement observer prerequisite

The historical observer queries SQLite transaction rows directly. It cannot read
a live RocksDB database through a second process without changing the consistency
contract. SP10c adds a diagnostic-only transaction marker stream to every measured
arm, including both SQLite configurations. Markers contain sequence, end LSN,
transaction ID and observation time, never payloads. They are emitted only after
a successful durable append, without adding a diagnostic fsync. Output is bounded
to 256 MiB. Missing, corrupt or incomplete required markers invalidate a trial;
markers never authorize feedback, replay or publication.

The source workload SQL, pacing, client count, source acknowledgements, publication
queries and final equality checks stay unchanged. New harness/package hashes make
the observer change explicit. Before drawing engine conclusions, compare accepted
SP10b plus its original SQL observer against identical SP10b with marker observation.
Keep observer on/off controls, including final two-table equality. Do not compare
unadjusted old SQLite observer timings directly with new RocksDB timings.

## Frozen comparisons and decision gate

Freeze code, dependency wheels, packages, harness and configuration before trials.
Retain every failed screen/trial and exact source identity; a fix creates a new
package/configuration, never edits an already measured arm.

1. Observer bridge: accepted SP10b versus instrumented SP10b on the historical
   twelve-trial matrix and qualified 8/16 CPU profiles. Measure observer on/off cost.
2. Engine attribution: instrumented SQLite owner versus RocksDB through identical
   SP10b IPC. Run the complete matched matrix, lifecycle/read fixtures and profiler
   activation/observer controls.
3. Product decision: instrumented best direct SQLite versus complete RocksDB owner
   design; same workload and instrumentation. IPC costs belong in this decision.
4. Sustained evidence: at least 30 minutes continuous offered load per backend,
   with multiple prune and compaction cycles. Record throughput, p95/tails,
   synchronous write time, native compaction/stall counters, capture/whole-stack
   CPU/RSS, physical disk high-water and write amplification. Reopen and verify the
   retained chain; bound resources with pinned readers and storage pressure.

A stage improvement alone is insufficient. Adopt only if a repeatable end-to-end
benefit resolves a remaining target or justifies the dependency/ownership cost.
Retain SQLite on neutral or worse results. Even a favorable result requires a
separate measured crash-safe migration slice and offline Linux/macOS release,
license and inventory qualification. SP11 and SP12 remain separate.

## Current evidence

All 31 Linux native contract, marker and IPC tests pass: replay/restart, retained prune
anchor, consistent view, cancellation/deadlines, exclusive ownership, format and
identity fences, corruption, rejected group atomicity, physical accounting,
backpressure, and process termination before/after append and prune commits.
The existing 77 performance-harness tests also pass. Installed lifecycle and
full-matrix qualification is underway. The unchanged SP10b IPC tests additionally
cover authority revocation, queue pressure, cancellation, response budgets and
slow readers against the RocksDB backend.
No performance improvement or completed SP10c qualification is claimed yet.


The first installed package omitted its extra wheel from the runtime locks;
[#151](https://github.com/supabricks/platform/issues/151) is fixed by `91ad97d`.
Package 01 and its failed lifecycle screen remain immutable. Package 02 also
exposed the notebook inventory hash and requirements formatting dependencies;
package 03 passes strict installation and environment verification. The lifecycle
and profile-accounting adaptations are tracked in
[#152](https://github.com/supabricks/platform/issues/152). No failed fixture is
silently replaced in a matched comparison.

The Linux RocksDB wheel is 4,206,954 bytes compressed and adds approximately
11.05 MB of installed files including the backend and marker instrumentation.
Package 03 assembly took 4.34 seconds on this host, reusing the accepted native
binary and base runtime. This is incremental overlay assembly, not a complete
source build or macOS package qualification.


## Frozen campaign launch

The corrected package passed installed range, lifecycle and observer on/off
screens. The lifecycle includes worker reuse/kill, pause/resume, whole-stack
restart, schema fencing and pinned historical epochs. All fixture cleanup reports
show zero remaining or leaked descendants. A 10-second resource/reopen plumbing
screen passed correctness but missed its offered input rate; it is explicitly
excluded from source-capacity and sustained qualification.
[Screen receipts, failures and package proofs](sync-performance-evidence/2026-10-03-sp10c-screen/README.md).

The supervised sequence uses immutable harness `0682839` and three complete
108-fixture campaigns: observer bridge, engine attribution, and product comparison.
Three additional 30-minute runs cover SQLite owner, RocksDB owner and direct
SQLite, for 327 planned accepted fixtures. The long-run profiler stays off to
respect its short-trial output budget; each arm uses the same bounded independent
CPU/RSS/I/O/disk sampler and exact transaction/publication latency observer, then
reopens and verifies the retained journal after all owned processes stop.

At launch there are **zero accepted performance trials**. The controller waits for
quiet-host admission, preserves failures and stops for investigation rather than
editing frozen artifacts. No adoption decision is automatic. Both the observer
cost and the engine/product results require review before SP10c is complete.


## Campaign stop, October 3

The observer bridge stopped after 14 accepted fixtures (six range, six lifecycle,
two observer controls). A further passing observer arm is unpaired and unaccepted.
The next accepted-SP10b SQLite observer-off run delivered all 187,500 transactions
at 1,249.958 changed rows/s but timed out waiting for final table equality.
Retained state shows 177 successful incremental runs, then `unsafe_journal_owner_path`
and a fenced capture. No build overlap was detected and descendant cleanup was clean.
Engine/product matrices and 30-minute runs have not started.

[#153](https://github.com/supabricks/platform/issues/153) records a deterministic
matching race: atomic replacement after opening control JSON can leave its inode
with zero links, which the owner incorrectly rejects as unsafe. The failure lacked
a stack identifying the exact rejecting check, so attribution to this mechanism
remains provisional. Preserve the stopped campaign; qualify a fix separately before
creating new comparison packages. Both native contract CI targets pass. The Linux
notebook environment lifecycle failure repeats the signature tracked in
[#135](https://github.com/supabricks/platform/issues/135).
