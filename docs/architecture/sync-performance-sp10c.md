# SP10c — controlled RocksDB comparison

Status: implementation and qualification in progress. SP10b merged in
[#145](https://github.com/supabricks/platform/pull/145), main commit `3a35d1a`.
No RocksDB adoption or migration is authorized by this experiment. Production
continues using SQLite; the experimental backend lives in the performance harness
and is selected only by a hashed, disposable installed-package overlay.

Sequence 02 completed all 324 comparison fixtures, then stopped on the first
30-minute SQLite sustained run at the 1,024 incremental history limit
([#156](https://github.com/supabricks/platform/issues/156)). The
[execution-history correction](sync-history-retention.md) is a separate measured
reliability slice. The two remaining sustained arms and final evidence review
remain outstanding; the original failed campaign is retained.

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

## Owner replacement correction (#153)

The correction discards unlinked or replaced JSON versions and rereads current
authority, bounded to eight attempts and three seconds. Request reads also check
the original channel deadline and cancellation on every attempt. Descriptor
metadata is checked before and after reading and compared with the current path;
unsafe permissions, foreign ownership, symlinks and multiple hardlinks remain
fatal. No stale JSON supplies authority. Existing pre-snapshot, pre-response and
pre-completion authorization fences remain in place.

Exhaustion is read-only contention: requests defer within the existing apply
budget, and incomplete responses cannot expose rows. Startup exhaustion reports
`unavailable/source_unavailable`, preserving the journal without source setup,
feedback or source cleanup. It does not declare a resync requirement. This fixes
the reproduced mechanism for both storage engines; it does not establish that
every observed campaign failure came from that check.

Qualify this as a separate logical slice, using the accepted SP10b SQLite package
against a package containing only this production correction, with identical SQL
observation, profiler and workload harness. Run installed screens first, then the
existing 108-fixture component/lifecycle, observer, historical, qualified-load and
profiler-control campaign. Preserve any failing predecessor arm and stop for
investigation. Do not combine these measurements with a RocksDB engine change or
resume the stopped sequence in place. Only after reviewing this correction's
measurements should new fixed-owner SQLite/RocksDB comparison packages be frozen.

Validation also exposed an intermittent experimental RocksDB resource-cycle
failure in physical-file accounting (`unsafe_spool_path`), tracked separately in
[#154](https://github.com/supabricks/platform/issues/154). Ten diagnostic repeats
did not reproduce it at that stage. The later retired-SST investigation and
correction below reproduce and resolve that mechanism while retaining the original
failure. Backend adoption still requires engine/product and sustained measurements.

Correction source `04d0c3c` passes 130 analytics tests and 16 RocksDB IPC tests.
The accepted package fails the deterministic replacement reproduction; the fixed
reader returns the current version. The new SQLite package changes only
`owner.py`, `capture_worker.py`, and their bytecode. Strict installation verification,
installed read/lifecycle checks, and observer off/on screens pass with no leaked
descendants. [Correction evidence](sync-performance-evidence/2026-10-03-owner-replacement/README.md).

The independent correction campaign completed under
`supabricks-sp10c-owner-fix-01.service`, using unchanged harness `0682839` and
immutable configuration `owner-fix-config-01.json`. Its 108 fixtures compare
accepted SP10b with the corrected SQLite owner. All 108 passed without failures,
contention replacements or leaks. Receipt reconstruction found no main regression
screen flags. The review retains the correction for reliability, without a speedup
claim. Isolated 32-record reads cost +4.78% latency and +6.64% CPU (paired medians).
[Reviewed results and limitations](sync-performance-evidence/2026-10-03-owner-fix-reviewed/README.md).

## Quiet admission for future campaigns

The user requested fewer repeated quiet waits and explicitly requested that the
current correction measurement not restart. Its frozen harness `0682839`, package
identities, configuration and running service therefore remain unchanged.

New campaign controllers retain a continuous host monitor across phases. A phase
inherits a bounded, atomic checkpoint only when it is from the same boot, has
recent samples and valid monotonic timestamps. It records that checkpoint and
its hash, takes its own current sample, and continues local contention monitoring.
A phase boundary alone adds no five-minute wait. Competing build activity or a
sampling gap resets quiet credit; missing or stale handoff evidence requires a
fresh interval. Comparison archives retain the handoff evidence as well as their
local host samples. Both measured arms use the same admission policy.

The old eight-phase correction campaign spends about 40 minutes establishing
quiet intervals even on an uncontended host. Continuous evidence reduces that to
one initial five-minute interval when monitoring stays healthy. It does not shorten
the declared warmup/load windows or run competing fixtures concurrently. This is
a future harness change, not a modification or reinterpretation of current results.

## Retired SST accounting correction (#154)

Native resource-cycle stress reproduced the rejected metadata: `000245.sst` was
a regular file owned by the worker UID, with `st_nlink=0` and an observed size of
542,778 bytes; a subsequent lookup returned `FileNotFoundError`. Compaction had
unlinked the file during path lookup/stat. The old accounting check required
exactly one link and falsely raised `unsafe_spool_path` after a durable write.

Physical accounting now accepts zero or one link for an owned regular file. It
conservatively counts the observed bytes for the current sample; it does not skip
the entry, reopen contents, retry a write, or grant replay/feedback authority.
Multiple hardlinks, symlinks, directories, foreign ownership and lookup permission
errors still fail. A retired file's observed bytes still enforce `spool_budget`.
Already-absent files retain the existing handling. This remains sampled physical
accounting, not a hard filesystem quota or a guarantee that a directory scan sees
every concurrently created file.

Seven deterministic accounting cases include real unlinked-inode metadata from
an open descriptor. The old implementation fails the retirement and budget cases;
the corrected implementation passes all 42 native contract/marker/IPC cases.
The focused stress harness repeats the existing native resource-cycle test with
50 accounting scans per call, recording any naturally observed retired files.
CI runs five repetitions independently on Linux and macOS. Qualification output
and installed screens are retained before the new experimental package is used.

Correction `dcca9cf` passed 20 local stress fixtures (455,000 accounting scans),
including 46 natural zero-link observations. Linux and macOS CI each passed the
42-test contract suite and five stress fixtures, observing another 17 and four
retired files respectively. The focused 32-file accounting comparison measured
104.323 µs before versus 104.412 µs after (+0.086%, three alternating pairs); this
does not establish pipeline speedup or statistical equivalence.

Experimental package `rocks-runtime-04` passes strict installation verification,
bounded reads, full lifecycle, and observer off/on screens with zero leaked or
remaining descendants. It includes the separately qualified owner correction
(108 accepted fixtures) as its base. Old packages and measurements are unchanged.
[#154 correction evidence](sync-performance-evidence/2026-10-03-rocks-accounting/README.md).
Engine/product comparisons and sustained qualification still precede adoption.

## Qualification sequence 02

The remaining qualification uses clean frozen harness `3fdebb0` (`harness-07`),
including continuous quiet-host evidence. The completed 108-fixture owner
correction is reviewed and archived; its original campaign was not restarted.

The new immutable configurations are `sequence-config-02.json` and the matching
observer-bridge, engine-comparison and product-comparison `-config-02.json` files.
Packages are corrected SQLite owner (`sqlite-owner-fix-runtime-01`), corrected
SQLite owner plus markers (`sqlite-owner-runtime-02`), corrected RocksDB owner
plus markers (`rocks-runtime-04`), and best direct SQLite plus markers
(`sqlite-direct-runtime-01`). Both owner arms share identical owner, worker and
marker source hashes. All marker arms share the same marker source. Source
revisions and package/binary identities are recorded separately from the harness.

The sequence contains three 108-fixture campaigns, then three 30-minute sustained
runs: **327 fixtures** in total, approximately 21–22 hours on an uncontended host.
It begins with the observer bridge, then same-interface engine comparison, then
comparison against direct SQLite. Each campaign retains component, lifecycle,
observer, profiler and matched pipeline controls. The qualified input is 1,250
changed rows/s with eight source clients; historical four-client cells remain
for comparison and are not treated as proof of offered-load capacity.

Service: `supabricks-sp10c-sequence-02.service`. Evidence root:
`build/sp10c-20261003/sequence-02`. The controller reports a ten-second heartbeat,
stops on fixture/cleanup/identity failures, and preserves rejected evidence.
Fresh continuous host evidence carries quiet credit across phase boundaries;
activity or sampling gaps reset it. This is the first full integration of #155.
A completed service still requires receipt/hash review and an explicit measured
keep/reject decision. Launch does not establish RocksDB speedup or adoption.

[Sequence 02 frozen configuration and launch evidence](sync-performance-evidence/2026-10-03-sp10c-sequence02/README.md).
