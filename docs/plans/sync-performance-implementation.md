# Synchronization performance implementation — SP00–SP12

[Plan index](README.md) · [Delivery status](status.md) ·
[Workflow profile](../architecture/sync-workflow-profile.md) ·
[CPU scaling history](../architecture/sync-core-scaling.md)

Status: **SP00–SP04 merged and measured; SP05 assessed, tuning deferred; SP06–SP12 planned**, 2026-09-26.
[SP00 report](../architecture/sp00-reproducible-comparisons.md) and
[PR #95](https://github.com/supabricks/platform/pull/95) retain 24 mandatory trials
and six follow-up trials; decision: keep for reliability/enabling, no runtime
speedup. [SP01](../architecture/sp01-journal-contention-recovery.md) adds bounded
read-only contention recovery: four observed overload resync failures become zero,
while all six overload trials still time out. Decision: keep for reliability/enabling;
no throughput improvement established. [SP02](../architecture/sp02-durable-capture-groups.md)
then reduces overload sync calls per captured transaction by about 93.4%, with all
six candidate overload trials completing at 684–689 actual changed rows/s. Decision:
keep for performance; the 1,000-row/s source input and sustained target remain unqualified.
[SP03a](../architecture/sync-performance-sp03a.md) verifies the existing fixed SQLite builds on Linux/macOS with no dependency upgrade: all 24 fresh comparison trials and 18 activation controls pass; no speedup is established. Decision: keep for reliability/enabling.
[SP03b](../architecture/sync-performance-sp03b.md) qualifies bounded capture WAL/FULL on both platforms: all 24 fresh main trials pass, achieved overload input improves 8–9%, native sync calls/transaction fall about 44%, and total CPU rises about 7–9%. All controls/ablations are retained. Decision: keep for performance; 1,000 rows/s remains unqualified.
This plan turns the workflow profile into separately measured slices. The existing SY00–SY08 correctness contract and qualified release
envelope remain authoritative until a new exact release passes qualification.

## Objective and scope

Achieve sustained **1,000 changed source rows/second with commit-to-publication
p95 at or below five seconds**, without losing acknowledged changes, exposing
partial multi-table transactions, weakening durability, or exceeding resource
budgets. The initial target is the existing two-table, two-changed-rows-per-source-
transaction workload on the reference Linux host at both 8 and 16 logical CPUs
(4 and 8 physical cores). Preserve the passing 50-row/s profile at 4 and 16
logical CPUs. Report the measured lower-core capacity rather than promising
linear CPU scaling. The reference host is the Ryzen 7 7800X3D (8 physical /
16 logical cores) with local NVMe/ext4; each trial restricts the complete stack to
16 GiB RAM. Exact topology, filesystem and package identities come from the
retained manifests, not the CPU model alone.

These are engineering acceptance targets, not current product guarantees.
Performance depends on transaction shape, row width, table size, storage, readers,
and concurrent work. The path is PostgreSQL → durable capture → incremental
Delta application → atomic analytical publication. Sail query execution time,
reverse sync, distributed HA, and EC2 sizing require separate qualification.

**Every logical slice must be measured before the next slice is accepted.**
A slice report compares its candidate with its immediate accepted predecessor
and with the immutable original baseline. A faster component, fewer failures,
and faster complete replication are distinct outcomes. A failed or inconclusive
experiment is retained and can end in rejection; implementation is not evidence
of improvement.

## Evidence and diagnosis

The [2026-09-24 profile](../architecture/sync-workflow-profile.md) and its
[raw archive](../architecture/sync-performance-evidence/2026-09-24-workflow-profile/README.md)
are the starting evidence. Analysis was committed at
`bc039090c609560dfb9ad676e339229f4e65e16f`; the report records the separately built
runtime, package, instrumentation and harness identities. Do not equate the
report commit with the tested binary. [PR #89](https://github.com/supabricks/platform/pull/89)
contains the profiling work.

| Finding | Measured evidence | Implication / tracking |
| --- | --- | --- |
| Serial durable capture | About 29–30 captured transactions/s under overload; 29.66–30.34 ms mean SQLite COMMIT; four native sync calls per commit; about 99.7% of commit elapsed time inside sync calls | Two changed rows/transaction requires 500 workload transactions/s. Amortize durability cost. [#87](https://github.com/supabricks/platform/issues/87) |
| Reader contention | Four overload trials fail with `SQLITE_BUSY` in the spool metadata SELECT after approximately 3.002 seconds | Retry the safe read boundary and improve reader/writer coexistence. [#88](https://github.com/supabricks/platform/issues/88) |
| Capture backlog without lock failure | Two trials exhaust the 120-second drain, with at least 8,469 and 8,527 uncaptured source commits | Removing the exception alone cannot meet throughput |
| Repeated apply directory scans | Successful workers within failed overload trials spend trial medians of 1.044–1.214 seconds checking directories versus 18.4–20.1 ms in Delta merges | Reduce repeated full scans while preserving resource/path checks. [#91](https://github.com/supabricks/platform/issues/91) |
| Source workload ceiling | Four-client source-only baselines reach 857.7–886.0 changed rows/s; COMMIT consumes 96.2–96.8% of measured SQL elapsed time | Establish source capacity independently; changing the spool cannot fix source WAL/replication waits |
| Freshness includes scheduling and startup | Passing 50-row/s trials have p95 3.68–4.94 seconds; cumulative admission p95 is about 2.2–2.9 seconds; imports approximately 0.2 seconds per worker | Profile scheduling and worker lifecycle after the primary constraints are addressed |
| More cores do not fix serial waiting | Capture uses approximately 0.024–0.025 CPU cores in matched overloaded intervals; total container use is approximately 1.08–1.29 cores | Introduce parallel work only when a measured stage can use it |

All six low-load trials passed correctness and freshness. All six overload trials
failed; they have no complete end-to-end latency percentile. Directory/merge
numbers under overload describe selected successful workers, not successful
pipelines. Native sync time includes filesystem, OS scheduling and device waits;
it does not isolate hardware service time. Stage percentiles and inclusive spans
must not be added together.

## Architecture decisions and invariants

Improve the existing capture spool first. Evaluate RocksDB as an isolated
alternative after establishing a durable, batched SQLite baseline. FoundationDB
is deferred to an explicit distributed-state/HA workstream; adding a replicated
transactional service is not a prerequisite for this local target. Its separate
transaction, log and storage roles change operations and failure handling.
[FoundationDB architecture](https://github.com/apple/foundationdb/wiki/Technical-Overview-of-the-Database)

Keep PostgreSQL source storage, Delta table storage and the SQLite publication
catalog in their current roles. A capture backend experiment must not silently
replace the publication authority or change the user-facing synchronization modes.

Every slice preserves these invariants:

1. Persist complete source transactions in order. Store payloads, checksums,
   predecessor links, barriers and the captured cursor atomically. Group commit
   preserves each source transaction's identity and boundary.
2. Replication feedback never exceeds the durably committed contiguous cursor.
   Decoded/in-memory progress is not acknowledgment authority. On uncertain commit
   outcome, reopen and verify durable state before advancing feedback.
3. Publication commits the complete table-version map and source cursor atomically.
   Individual Delta table commits are preparation, not permission to expose a
   partially updated group. Existing readers remain pinned to their original epoch.
4. Prune only the prefix authorized by durable publication, preserving reconnect
   verification anchors and reader/recovery references. Acknowledged unpublished
   data must survive restart and backend migration.
5. Retain source identity, bootstrap handoff, schema checks, bounded memory/disk/
   WAL retention, generation fencing, path protections, and governed service
   authority. Pressure pauses/fails explicitly; it never drops changes or evicts
   pinned data to manufacture a passing benchmark.
6. Preserve `synchronous=FULL` or equivalent synchronous durable writes. No
   `fsync` disabling, asynchronous source commit, unlogged source tables, missing
   WAL, or weakened replication policy in a performance comparison.
7. Process-kill tests establish process-crash behavior. They do not establish
   physical power-loss or machine-failure qualification.

## Measurement contract for every slice

### Immutable matched matrix

Run the same four cells, three repeats each, after **every runtime, dependency,
configuration, or measurement slice**, including optional sub-slices:

| Logical CPUs | Physical cores on reference host | Offered changed rows/s | Repeats |
| --- | --- | --- | --- |
| 4 | 2 | 50 | 3 |
| 16 | 8 | 50 | 3 |
| 8 | 4 | 1,000 | 3 |
| 16 | 8 | 1,000 | 3 |

Hold the original workload fixed: four source clients with disjoint keys; two
10,000-row tables; one changed row in each table per transaction; five-second
source-only baseline; five-second warmup and catch-up; 45-second measured load;
120-second drain; 16 GiB container memory with no swap or CPU quota; complete SMT
pairs; fresh isolated fixtures; the same filesystem and observer behavior.
Capture/control transactions are reported separately from workload transactions.
A changed row means a source row-change event: repeated changes to the same key
still count, even when apply coalesces them. Do not use final table row count or
Delta rows rewritten as replication throughput. Attribute each measured committed
transaction to its first complete covering publication, and disclose observation
resolution and clock domains. Use monotonic clocks for elapsed durations; wall
clock/source timestamps need the existing same-host correlation checks.

The four-client matrix remains mandatory even if the source cannot generate
1,000 rows/s. It measures historical comparability; it cannot prove capacity at
an input rate that was not achieved. Additional source-qualified profiles below
are separately named and never replace these twelve trials.

For each slice:

- Freeze source, configuration, package, dependencies, workload seed, harness and
  profiler versions before measurement. Never edit a running series.
- Run **12 candidate trials plus 12 fresh predecessor trials**, pairing each cell
  and repeat in seeded, balanced A/B or B/A order. Run sequentially, never two
  stacks at once. Keep the original archive as a separate historical reference;
  fresh predecessor runs control for host drift.
- Require five quiet minutes before each trial. Retain CPU, memory, I/O pressure,
  filesystem/free-space, temperature/frequency where available, and external-build
  observations. If contention invalidates a trial, wait and rerun the affected
  pair; retain both attempts. Never stop another project's build or the user's stack.
- Retain runtime failures as outcomes. Only predefined measurement/environment
  invalidity permits replacement. Cleanup failure stops the series.
- Use the same profiler in both arms. Requalify instrumentation changes against an
  unchanged runtime first. Use three profiling-on/off pairs when instrumentation
  changes, and for the final candidate; include overload activation controls once
  it completes. Add observer-disabled throughput controls at capture/backend and
  final milestones, with independent final correctness verification.
- Run the slice's focused component benchmark and correctness/fault tests before
  the full matrix. Capture-only tests include real durable writes, pruning and
  bounded readers; memory-only replay numbers are not durable throughput.
- Once SP06 establishes a source-qualified load profile, run its three repetitions
  at both 8 and 16 CPUs on both arms after each subsequent runtime slice as well.

Any tuning of batch limits, polling cadence, worker count or checkpoint thresholds
is its own recorded experimental variant. Freeze the selected setting before its
acceptance matrix. Do not combine a backend change with a client-count change or
report their combined gain as the backend's contribution.

### Attribution, decisions and reports

For every cell, retain all three individual results, median/range, paired absolute
and percentage changes, and failure counts. The experimental unit is a trial,
not millions of correlated rows. Three repeats are screening evidence; repeat
another balanced block if the result is close to host noise, inconsistent, or
sensitive to order. Do not claim statistical certainty from three runs.

Record:

| Boundary | Required metrics |
| --- | --- |
| Source | Offered, attempted and committed rows/s; workload transactions/s; SQL COMMIT distribution; PG wait events; safekeeper flush and WAL statistics |
| Capture | Complete transactions and bytes/s; group count/bytes/age; durable commit time; sync calls and sync time per committed transaction and group; decoded/durable/feedback cursors; receive/decode/status/prune costs |
| Journal readers | Read duration, contention codes, retry count/wait, deadline exhaustion and snapshot lifetime |
| Apply | Rows/bytes per batch; planning/self time; directory traversals/stat calls; imports/startup; Delta merge, durability, inventory and maintenance time; rewritten bytes |
| Publication | Admission/queue delay, worker dispatch/start, preparation, verification, descriptor durability and atomic catalog commit |
| End to end | Generated and published changed rows/s; commit-to-first-covering-publication p50/p95/p99; backlog bytes/transactions/age over time; drain/catch-up rate; full correctness |
| Resources and reliability | Total cgroup CPU/RSS/I/O/pressure, process deltas, disk/WAL/spool/retained-file peaks, checkpoint/compaction cost, crashes, resyncs and cleanup |

Incomplete trials have no complete latency percentile. Compare their backlog,
progress and time-to-failure without extrapolating a successful latency. A change
from failure to completion is a reliability gain, not a percentage speedup.
Upstream improvements can expose a slower downstream stage; report that explicitly.

Each logical slice receives one of these decisions:

- **Keep — performance:** targeted stage improves repeatably, correctness holds,
  and there is no unexplained end-to-end or resource regression.
- **Keep — reliability/enabling:** removes a failure or enables a necessary
  experiment; state explicitly that no speedup was established.
- **Inconclusive:** extend paired measurement or improve attribution before taking
  credit. A necessary correctness fix may remain, labeled as such.
- **Reject/revert:** no useful benefit, excessive cost, or violated correctness.
  The next experiment starts from the last accepted predecessor.

Investigate a paired median latency/CPU/RSS regression greater than 10%, or any
new low-load freshness miss, runtime failure, or unbounded growth. Ten percent is
an investigation trigger, not permission to ignore smaller repeatable regressions.
Do not relax the five-second target to accept a change. Check interactions with
one-feature-disabled ablations after combining batching/WAL and before final
qualification; rerun the matched matrix for each ablation.

Archive under
`docs/architecture/sync-performance-evidence/<date>-spNN[-variant]/` with a human
report, immutable manifests, all attempts, raw structured profiles, host and cleanup
records, analysis source, checksums and a machine-readable comparison. Proposed
comparison fields: slice/variant ID, hypothesis, predecessor/candidate identities,
changed settings, per-trial/cell metrics, marginal and original-baseline deltas,
failures, validity, decision and next constraint. Sanitize credentials, source
values, SQL text and private paths before publishing.

Maintain a contribution ledger in the performance architecture report:

| Slice | One changed mechanism | Predecessor / candidate | Targeted-stage delta | Complete pipeline delta / failures | Resource cost | Decision / evidence |
| --- | --- | --- | --- | --- | --- | --- |
| SP00 | Measurement reproducibility | `bc03909` / `f0a93ed`, same diagnostic package | Capture remains ~30 transactions/s, four syncs/commit | Mandatory low-load complete 6/6 per arm, fresh 6/6 predecessor and 5/6 candidate; overload complete 0/6 per arm; six additional low-load trials all pass | Mandatory low-load CPU −2.01% / +0.72%; peak memory +0.13% / −1.19% | [Keep — reliability/enabling; no speedup](../architecture/sp00-reproducible-comparisons.md) |
| SP01 | Bounded pre-mutation journal-read retry | Runtime `9227275` / `b6a0b4b`, shared harness `c52f466` | Mandatory candidate fixtures recover 477 BUSY responses across 260 completed reads; bounded deferral/exhaustion verified by fault tests | Mandatory low-load complete 6/6 per arm, fresh 5/6 predecessor and 6/6 candidate; overload complete 0/6 per arm, resync failures 4 → 0 | Paired median CPU +3.47% / +2.64% at low load, +4.68% / +4.42% under overload; no material memory regression observed | [Keep — reliability/enabling; no throughput gain](../architecture/sp01-journal-contention-recovery.md) |
| SP02 | Bounded durable capture group commit, retaining FULL/DELETE | `485b552` / `838f0b1`, common probe/harness | Overload capture ~30 → 343–344 transactions/s; paired sync calls/transaction −93.38% / −93.48%, durable time/transaction −93.50% / −93.63% | Low-load correct/fresh 6/6 per arm; overload completion 0/6 → 6/6 at 684–689 actual changed rows/s, p95 4.49–4.63 s; 1,000-row/s input not achieved | Paired median CPU +3.67% / +4.14% at low load, +7.91% / +10.92% under overload with far more capture/apply work completed; memory costs retained | [Keep — performance](../architecture/sp02-durable-capture-groups.md) |
| SP03a | Loaded SQLite identity and release qualification, retaining SP02 FULL/DELETE | Accepted SP02 `838f0b1` (merged `a88eb145`) / `da548e7`, shared frozen harness | Python 3.53.1 and Rust 3.53.2 already fixed on Linux/macOS; no dependency upgrade; overload syncs/transaction within +0.89% paired median | All 24 mandatory trials correct/fresh; 18 controls and nine component trials pass; four contended trials retained/replaced; 1,000-row/s source input still unmet | Paired median CPU +0.08% to +0.79%, memory −2.87% to +2.68%; diagnostic binary +54,544 bytes, policy +713 bytes | [Keep — reliability/enabling; no speedup](../architecture/sync-performance-sp03a.md) |
| SP03b | Capture-only WAL/FULL, owned checkpoints and physical admission; SP02 grouping retained | SP03a `da548e7` (merged `43c046e`) / `5382e80`, common frozen harness | Overload native syncs/transaction −43.87% / −43.83%; COMMIT + checkpoint time/transaction −46.71% / −46.37% | All 24 main trials correct/fresh; achieved overload medians 742/746 rows/s (+8.12%/+9.08% paired); 1,000 input unmet. Grouping ablations: three DELETE timeouts, three WAL freshness misses without grouping; all grouped runs pass | Main paired CPU +6.71% to +9.11%, peak memory −4.80% to +5.31%; conservative DB/WAL headroom; bounded transient-reader WAL allocation tracked in #116 | [Keep — performance; retain grouping](../architecture/sync-performance-sp03b.md) |
| SP04 | Two owned planning inventories with bounded per-batch checks; existing output/durability checks retained | SP03b `5382e80` (merged `f02dca8`) / `e10d515`, common frozen harness | Matched aged directory checks −98.27%/−98.96%; planning walks 206–800 → 2; fresh checks −79.38% to −82.49% | All 24 main + 36 controls correct/fresh and 24 component plans equal; main paired p95 −32.59% to −39.72%; achieved overload input −2.32%/−2.82%, 1,000 input unmet | CPU −12.56% to +9.47%, peak RSS −2.57% to +4.48%; faster worker frequency exposes startup/publication costs in #119 | [Keep — latency, with resource/input tradeoffs](../architecture/sync-performance-sp04.md) |
| SP05 | Offline maintenance triage; no runtime/profiler/harness change | Accepted SP04 `e10d515` (merged `c8e23e2`); existing 24 main trials reused | Candidate checkpoint 0.73–1.10% and prune 0.23–2.21% of covered wall time; inclusive spans overlap; no compaction exercised | No new benchmark trials or speedup; source target remains unmet; sustained rotation/reader-pressure performance unqualified | Observed spool <1.12% of budget with no busy/backpressure increments in short main runs; #116/#119 remain open | [Defer tuning — assessment complete; sustained cases required in SP11](../architecture/sync-performance-sp05.md) |

No cumulative result may omit rejected attempts or credit all gains to the last
change. No runtime slice is complete until its report and ledger row exist.

## Delivery sequence

The numbered slices below are ordered review and measurement units. PR boundaries
may be larger only if they retain separately reproducible commits/packages and
completed reports for each logical slice. Do not begin accepting the next runtime
mechanism until the predecessor's measurement decision is recorded. Dates and
implementation estimates follow SP00; measurement and fault qualification are
part of the work, not work left until the end.

| Slice | Change / dependency | Main exit question |
| --- | --- | --- |
| SP00 | Reproducible comparisons and baseline; first | Can we attribute a change without moving the workload or host conditions? |
| SP01 | Bounded journal-read retry; SP00 | Do transient locks stop causing unnecessary resyncs? |
| SP02 | Durable capture group commit, retaining DELETE journal; SP01 | How much do fewer durable commits contribute? |
| SP03a | SQLite dependency qualification/upgrade if required; SP02 | Is the actual bundled library safe for the proposed WAL profile? |
| SP03b | Capture SQLite WAL + FULL; SP03a | What additional gain comes from journal mode and reader concurrency? |
| SP04 | Reduce repeated full directory scans; SP03b | Does apply cost stop growing with scan batches times retained files? |
| SP05 | Checkpoint/pruning/maintenance tuning, only if measured; SP04 | Can sustained resource bounds hold without latency spikes? |
| SP06 | Sustained source capacity and separately frozen load profile; SP04/05 | Can the source supply the target independently? |
| SP07 | Source-side fix only if SP06 proves it necessary | Which measured source wait prevents sufficient input? |
| SP08 | Scheduling/dispatch latency, if still material | Does freshness improve without more empty work or weaker admission? |
| SP09a/b | Worker reuse and then table parallelism, independently gated | Do startup or CPU-bound apply work now justify more complexity? |
| SP10a/b/c | Capture interface, shared-owner access, RocksDB experiment | Does a backend replacement outperform the improved SQLite path fairly? |
| SP11 | Sustained, recovery, resource and scaling qualification | Does the result survive aging, failures and realistic contention? |
| SP12 | Exact installed release qualification and documentation | Which precise build/profile can we support and claim? |

SP00–SP04, SP06, SP11 and SP12 are required. SP03a may be a verification-only
step if the bundled libraries already contain the fix. SP05 and SP07–SP09 require
measured justification. SP10 is an explicit experimental branch, not a commitment
to shipping a new dependency. A skipped optional slice needs a recorded rationale;
an implemented sub-slice always needs its own measurements.

### SP00 — Reproducible baseline and comparison runner

Implemented and measured in [PR #95](https://github.com/supabricks/platform/pull/95);
[report, unexpected-tail investigation and evidence](../architecture/sp00-reproducible-comparisons.md).
Decision: keep for reliability/enabling. No runtime performance target is qualified.

Primary code: `e2e/native/performance/{matrix,trial,profile_trial,summarize}.py`,
`worker_profile.py`, `profile_package.py`, and archived orchestration/analysis.

Promote the archived matched-profile orchestration into a supported repository
runner. Add an explicit cell list so the four-cell matrix cannot accidentally
become the default Cartesian product. Add paired predecessor/candidate package
inputs, immutable manifests, restart validation, bounded host monitoring and
comparison output. Preserve the observer's attribution and cleanup checks.

Separate harness-only changes from runtime changes. Reproduce the original runtime
with the old and new harnesses and establish instrumentation consistency; retain
both manifests. Add meaningful accounting checks for missing markers, failed
trials, incorrect pairing, changed input rate, malformed profiles and cleanup.
Add group/checkpoint/retry counters before using them to judge those optimizations;
measure any required probe overhead against unchanged behavior. Design bounded
profile rotation/aggregation for long runs; retain incomplete tails and reject
missing required streams or exhausted evidence budgets. Do not silently disable
profiling when moving from 45 seconds to an hour.

Exit: the twelve-trial outcomes can be reproduced and compared, with no
false successful percentiles or hidden input shortfall. Original failure rates
need not reproduce exactly, but discrepancies must be explained before proceeding.
Do not regenerate or replace the original archive.

### SP01 — Safe journal-read contention recovery

Primary code: `python/analytics/incremental/storage.py:journal`, worker error
classification and supervisor handling in `crates/local/src/store/incremental.rs`.
Track [#88](https://github.com/supabricks/platform/issues/88).

Retry only recognized transient SQLite busy conditions at the read-only journal
boundary. Roll back/close the failed read transaction and reopen a consistent
snapshot for the same immutable identity/after/target request. Use bounded
backoff within one overall monotonic deadline; each SQLite busy timeout must fit
inside the remaining budget. Do not multiply the current three-second timeout
by an unbounded number of attempts. Treat `SQLITE_LOCKED` separately unless its
specific cause is proven retryable.

Exhaustion before Delta mutation should produce an explicit retryable/deferred
outcome, with bounded supervisor attempts. Check the existing initialization and
compaction path before declaring the whole worker replay-safe. Never retry an
entire potentially partially applied worker blindly. History loss, corruption,
identity mismatch, storage errors and uncertain mutations retain their distinct
recovery/fencing behavior.

Measure deliberate short and deadline-exceeding writer locks; metadata and payload
reads; pause/cancel/revocation during backoff; pruning between attempts. Exit:
transient contention recovers without data loss or unnecessary resync, while
persistent contention is bounded and visible. Throughput may remain unchanged;
that is an acceptable reliability result, not a speedup claim.

### SP02 — Bounded durable capture groups

Implemented and measured in [PR #103](https://github.com/supabricks/platform/pull/103);
[report, controls and contribution](../architecture/sp02-durable-capture-groups.md).

Primary code: `python/analytics/capture/spool.py`, `capture_worker.py`,
`capture/protocol.py`, capture status and failpoints. Track
[#87](https://github.com/supabricks/platform/issues/87).

Add an atomic `append_many`-style operation over complete transactions. Validate
ordering, replay checksums, identity and capacity for the entire group; write
individual records plus captured cursor/bytes/barrier metadata in one SQLite
transaction. Update in-memory progress only from the successful durable result.
Keep the existing DELETE journal and FULL setting in this slice to isolate batching.

The receive loop accumulates bounded complete transactions outside a SQLite write
transaction. Flush on count, bytes, or oldest-pending age, and promptly on a
triggered barrier or orderly shutdown. Socket waits must honor the group deadline;
the existing 200 ms poll must not override a smaller timer. Keepalives acknowledge
only the previous durable cursor while a group is pending. A partial oversized
source transaction cannot force unbounded buffering or be partially committed.

Initial experimental settings, not promised production defaults: 32 transactions,
1 MiB payload, 10 ms maximum accumulation age, whichever triggers first. A single
supported transaction larger than the group byte target uses a dedicated group
within the existing 4 MiB transaction limit. Bound aggregate pending memory and
physical write reservations separately. Screen count 8/32/128 and age 0/5/10/25 ms
one axis at a time; pick and freeze settings before the acceptance matrix. Record
actual group sizes at both 50 and 1,000 offered rows/s. The age limit bounds
accumulation, not the duration of a blocked durable commit.

Fault tests: death before group transaction, before/after commit, before/after
feedback, mixed duplicate/new groups, changed duplicate checksum, reconnect after
pruning, barriers inside groups, disk exhaustion and ambiguous I/O completion.
No feedback may cover a missing record after restart.

Exit: reduced sync calls and durable time per source transaction with unchanged
replay semantics. Component target is at least 500 workload transactions/s for
this shape, preferably 750 for headroom; missing that target identifies remaining
work, not permission to weaken durability. Run the mandatory full comparison even
if apply or source remains limiting.

### SP03a — Qualify the SQLite dependency

Record the actual SQLite version/build loaded by the packaged Python workers on
Linux/macOS, plus Rust and qualification-reader versions where they access the
same spool. Do not infer them from the host `sqlite3` executable.

Before enabling WAL, require a version containing the WAL-reset race fix. SQLite
identifies 3.51.3 and later, with backports including 3.44.6 and 3.50.7. Verify the
actual pin/backport against the official advisory at implementation time.
[SQLite WAL-reset advisory](https://www.sqlite.org/wal.html#walresetbug)

If an upgrade is necessary, build and qualify it with DELETE journaling still
selected. Measure that dependency-only slice before SP03b; account for binary
size, reproducible native builds and package inventory changes. If no upgrade is
needed, retain the verification evidence and unchanged-runtime comparison.

### SP03b — WAL for the capture spool

Merged in PR #111; **keep — performance**. [Policy, qualification and measured attribution](../architecture/sync-performance-sp03b.md). All 24 main trials are correct/fresh; 36 full-stack profiler controls, 12 grouping ablations and 36 component/control trials are retained. Achieved overload input improves by 8–9%, but 1,000 rows/s remains unqualified. Native sync calls per transaction fall about 44% including checkpoint work; total CPU rises about 7–9%. Both installed platform gates pass. Keep grouping with WAL; the ungrouped variants miss freshness. SP05 should follow up the bounded retained-WAL allocation observed in [#116](https://github.com/supabricks/platform/issues/116).

Change the capture spool only to WAL with FULL durability, retaining SP02 group
settings. SQLite documents concurrent readers/writer and commit-time WAL syncing
with FULL, but WAL can still return busy and needs checkpoints.
[SQLite WAL behavior](https://www.sqlite.org/wal.html)

Check the returned journal mode and per-writer durability settings. Establish one
checkpoint owner, a bounded initial checkpoint policy and short reader snapshots;
keep tuning beyond safe initial settings for SP05. Count database, WAL, shared
memory and temporary files in physical budgets: `max_page_count` alone does not
bound the spool's footprint. Reserve checkpoint/write headroom and handle a
reader that prevents reclamation by bounded backpressure rather than disk filling.

Validate sidecar ownership/permissions/no-symlink handling, governed access,
read-only readers and backups. A changing WAL database must never be opened with
`immutable=1`. Close readers before Delta work so they do not pin WAL unnecessarily.
Migration requires quiescing both writer and readers, verifying identity/cursors,
then changing mode under ownership. Restart, backup, restore and rollback must
preserve committed WAL; copying only the database or deleting sidecars is invalid.
Downgrade to DELETE only through SQLite after successful checkpoint/quiescence.

Exit: paired attribution of WAL over batching alone, no reader consistency failure,
bounded sidecars, and preserved crash/restore behavior on both supported platforms.
Run a batching-on/off by DELETE/WAL component factorial, plus end-to-end ablations,
to distinguish interacting improvements from additive assumptions.

### SP04 — Bound filesystem work during apply planning

Merged and qualified in [PR #117](https://github.com/supabricks/platform/pull/117).
[Report and evidence](../architecture/sync-performance-sp04.md): all 84 declared
measurements pass; main paired p95 improves 33–40%. Keep for latency, with 16/50
CPU +9.47% and achieved overload source input −2.32%/−2.82% explicitly accepted
as measured costs. [#119](https://github.com/supabricks/platform/issues/119) tracks
fixed startup/publication work; 1,000 rows/s remains unqualified.

Primary code: `python/analytics/incremental/storage.py:{boundary,files}`,
`incremental_worker.py:plan`, inventory/durability callers. Track
[#91](https://github.com/supabricks/platform/issues/91).

Split cheap deadline/cancellation/value-budget checks from full generation
inventory checks. Build one validated inventory at a safe admission boundary;
reuse conservative accounting under the generation's exclusive mutation lease.
Perform full validation at defined mutation/publication boundaries and reconcile
new files and reservations after writes. Keep free-space checks at bounded
intervals and before allocating output. Invalidate cached accounting after
compaction, recovery, lease change or unknown filesystem mutation.

Remove the full recursive scan from each 32-row Arrow batch; retain bounded
per-batch checks. Do not change Arrow batch size, merge algorithm, worker lifecycle
or parallelism in this slice. Preserve protection against symlink/path replacement
and concurrent mutations; a stale cache is not evidence a path is safe. If exclusive
ownership cannot be established, use conservative revalidation instead.

Measure fresh and aged generations at equal file counts, scan batches and touched
keys. Count full traversals and stat calls, including unchanged safety work outside
planning. Target full traversal count proportional to mutation/publication
boundaries rather than Arrow batch count; seek at least an 80% reduction in the
measured planning directory-check cost at matched sizes. This is a diagnostic
target, not a promise that total apply time drops by 80%.

Exit: improved matched planning cost and no weakened deadline, ENOSPC, reservation,
retention or unsafe-path checks. Hold old readers through growth/maintenance tests.

### SP05 — Sustained storage maintenance, if needed

**Assessment complete; tuning deferred.** [Report and reproducible analysis](../architecture/sync-performance-sp05.md)
find no demonstrated maintenance bottleneck in the accepted SP04 short profiles.
No runtime or measurement change and no speedup claim. Follow the conditional
exit below: repeated reclamation, actual rotation and pinned-reader pressure
remain required in SP11; #116 and #119 remain open. A later policy change needs
its own SP05 sub-slice and full fresh comparison.

Use the new profiles to determine whether checkpoints, spool pruning/vacuum,
Delta compaction, inventory sealing or retained-reader pressure now dominate.
Change one mechanism per sub-slice (SP05a, SP05b, etc.), each with its own complete
comparison; do not combine checkpoint and Delta compaction tuning.

Candidates include moving bounded checkpoint work off a critical append boundary,
reducing redundant progress/prune scans, and selecting maintenance points from
measured space pressure. Preserve one owner, physical reservations, reconnect
anchors, published cursor authority and pinned epochs. Keep configured limits
visible and unchanged unless a separate resource-policy slice justifies them.

Measure at least three checkpoint/reclamation cycles and a generation rotation,
plus a pinned reader that delays cleanup. Exit: resource use stabilizes within
budgets without hidden long-tail stalls. If short profiles show no bottleneck,
record a deferral and exercise these cases in SP11.

### SP06 — Source capacity and a separate target-load profile

Keep the mandatory four-client comparison unchanged. Add source-only runs with
4, 8 and 16 clients, disjoint key ranges, unchanged two-row transactions and the
same durable PG/Neon settings. Use 60-second warmup and at least five-minute
measurement, three repeats per selected CPU/client configuration. First screen
at 8/16 CPUs; retain relevant 4-CPU measurements for the scaling envelope.

Measure committed input, COMMIT tails, SyncRep/WAL waits, safekeeper flush time,
CPU and disk utilization. Find the smallest fixed client count that sustains the
required source input; 1,250 rows/s is the preferred source-only headroom target,
not a product requirement. More clients are an experiment, not an assumed fix.

Freeze this client count and all workload settings in a separately versioned
source-qualified profile. Run both the immediate predecessor and candidate with
it to avoid crediting generator concurrency as a capture improvement. Clearly
label a harness-only change as enabling measurement, not faster runtime.

Exit: either independently proven input capacity, or an attributed source limit
and a scoped SP07 issue. No 1,000-row/s replication claim is allowed when the
source supplied less than the claimed rate.

### SP07 — Fix a proven source bottleneck, conditionally

Proceed only with SP06 evidence. Map waits to the exact PG/Neon/safekeeper path
before modifying it. If concurrency alone supplies sufficient input, record this
slice as unnecessary. If a source change is required, open/link its repository
issue with profiles, workload and affected versions.

Candidate investigations: existing group-commit behavior under concurrent clients,
redundant flushes, synchronous message serialization and serial CPU work. Select
one proven mechanism per SP07 sub-slice, keeping source durability, safekeeper
replication semantics and transaction shape unchanged. A source component change
requires its own crash/recovery tests, source-only comparison, complete stack
matrix and renewed component pins. Never substitute larger source transactions
without labeling a separate workload experiment.

Exit: source-qualified input is available with quantified OLTP latency/resource
cost. If not achieved, keep the overall target open and document the limit.

### SP08 — Remove measured scheduling delay, conditionally

Primary code: continuous supervision and admission in
`crates/local/src/store/{sync,incremental}.rs`, `crates/local/src/sync.rs`, daemon
worker dispatch and capture progress signaling.

Separate intentional micro-batch wait, stale progress observation, scheduler tick,
queue wait and worker launch. Change one source of delay per sub-slice: for example,
notification of newly durable capture instead of delayed polling, followed by a
separate batch-interval experiment if justified. Reuse durable state as authority;
a notification can be lost or duplicated and must not be the only recovery path.

Retain one publication writer, fairness across policies, revision/authority checks,
backpressure, cancellation and no-change behavior. Test idle CPU and control-plane
responsiveness so lower lag does not buy constant polling or empty publications.
Exit: a measured reduction in admission/dispatch delay at matched load, with no
freshness regression from smaller, more expensive batches.

### SP09a — Worker reuse, only if startup remains material

Reuse a bounded worker for the same authorized generation only if imports/startup
still consume a meaningful fraction of the target. Give it explicit request IDs,
per-request budgets, cancellation, memory limits and deterministic recycling.
Revalidate authority, generation identity and epoch on every request. Do not carry
credentials, mutable Delta handles or cached state across incompatible scopes.

Compare cold and warm startup, total CPU/RSS, request tails, idle cost, revocation,
worker crash and memory growth. Retain process isolation for unrelated projects.
Exit: demonstrated net pipeline benefit, with no lifecycle/security regression.
Otherwise retain short-lived workers.

### SP09b — Bounded table parallelism, only if apply becomes CPU-bound

Keep source ordering and one in-flight publication per sync group. Parallelize
independent table preparation/merge tasks within a batch only after profiling
justifies it; publish only after every table is durable and verified. Enforce
aggregate memory, disk reservations, threads and process limits. Avoid multiplying
Arrow/Delta/native thread pools by worker count.

Compare concurrency 1/2/4 as separate variants at matched CPU affinity. Kill/fail
one table task while others finish; prior published epochs must remain intact and
recovery must reuse verified work safely. Include multi-table atomicity, hot-key
and skewed-table workloads. Exit: throughput scaling or lower preparation time
that survives aggregate resource accounting. Do not add parallelism merely to
increase CPU usage when storage remains the constraint.

### SP10 — Controlled RocksDB experiment

Complete the improved SQLite comparison first. RocksDB provides atomic write
batches, synchronous writes and consistent snapshots, but ordinary read/write
ownership is within one process. Its group commit combines compatible concurrent
writes; it does not proactively wait to accumulate a batch.
[RocksDB operations](https://github.com/facebook/rocksdb/wiki/Basic-Operations),
[WAL behavior](https://github.com/facebook/rocksdb/wiki/WAL-Performance).

Run three separate logical slices, each with the full measurement gate:

- **SP10a — backend contract:** extract bounded append-group, durable cursor,
  snapshot/range-read, verify and prune operations. Keep SQLite implementation and
  current process access unchanged. Prove format/replay parity and measure abstraction
  cost. Identity, ordering and feedback authority remain outside backend-specific code.
- **SP10b — single-owner access:** implement a private bounded local IPC path with
  SQLite still underneath. Capture owns writes; readers request validated bounded
  ranges/snapshots through that owner. Bound request/response bytes, reader lifetime,
  queueing and cancellation; enforce project/generation/authority fencing. Measure
  serialization and IPC cost independently. Do not introduce an exposed network service.
- **SP10c — RocksDB backend:** use the same ownership/interface, payloads, group
  thresholds and workload. Atomically store records plus cursor/barrier metadata
  in a synchronous WAL-enabled WriteBatch. Implement replay hashes, pruning anchors,
  snapshot reads, physical disk accounting, bounded memory, compaction and restart.
  Standard secondary/read-only modes are not assumed to match our live consistency
  contract; any alternative requires its own proof and measurement.

Compare SQLite and RocksDB through identical IPC for engine attribution, then
compare the complete RocksDB design against the best direct-access SQLite design
for the product decision. Measure sync time, tail latency, write amplification,
compaction stalls, CPU/RSS, disk footprint, package size and build time. Include
at least 30 minutes of sustained load and multiple compaction/prune cycles in
addition to the matched matrix. Do not benchmark RocksDB with `sync=false` or
WAL disabled against durable SQLite.

Adopt only if it resolves an unmet throughput/resource target or gives a repeatable
material advantage worth the added dependency and ownership complexity. A suggested
screen is at least 20% improvement in the constrained stage, with no low-load
freshness or resource failure; stage gain alone is insufficient if the complete
pipeline gains nothing. Record and retain SQLite if RocksDB is neutral or worse.

Any adoption requires a separate measured migration sub-slice: quiesce ownership,
copy/verify the retained contiguous journal including acknowledged unpublished
changes, persist a format marker and switch atomically. Preserve the old backend
until rollback is safe. Crash-test every switch boundary; never silently discard
an acknowledged spool and attempt a new bootstrap. Package native bindings for
Linux/macOS and qualify offline installs/licenses/inventories. FoundationDB remains
out of this experiment because distributed durability is a separate requirement.

### SP11 — Sustained correctness, recovery and scaling

Run on the selected implementation, with the matched matrix and source-qualified
profile retained separately. If a failure needs a code fix, give that fix its own
logical slice and full comparison before restarting the affected qualification.

Required additional experiments:

- Carry forward the [SP05 deferred maintenance assessment](../architecture/sync-performance-sp05.md).
  Per required maintenance run, require at least three successful checkpoint and
  reclamation cycles plus a real generation rotation; extend duration until they
  occur. Repeatedly pin/release bounded SQLite snapshots and hold a Sail epoch
  across rotation/GC. Archive reader lifetimes, incomplete checkpoints, physical
  DB/WAL/sidecar and retained-root high-water marks, reuse and post-unpin cleanup.
  Predeclare stable-storage criteria and early/late windows. Request counts or
  maintenance-base lookups do not prove successful cycles. Keep #116/#119 open
  until their scoped evidence is obtained; isolate any fix as a new measured slice.

- Three 15-minute steady target-load runs at each of 8 and 16 logical CPUs, then
  at least one 60-minute run at each. Use the frozen source-qualified client count
  and at least 60 seconds of warmup. Include checkpoint/pruning and generation
  compaction/rotation; extend duration if the required cycles have not occurred.
- Measure actual generated and published rates. Offer sufficient input to prove
  at least 1,000 rows/s is committed and published during the steady measured
  interval, rather than relying on the offered-rate label. Archive pacing settings
  and rate windows; no long post-run drain may disguise accumulating backlog.
- Require p95 at or below five seconds in every complete trial; report p99/max
  and worst five-minute-window p95. Require stable backlog with no sustained
  increasing trend, and a final drain within the existing 120-second limit.
  Predeclare backlog analysis windows and tolerances in the run manifest; compare
  early/late windows and the time-series slope. Report maxima as well as averages
  so periodic checkpoint or compaction bursts cannot hide growing lag.
- Three-repeat capacity sweeps at 4/8/16 CPUs, with rates around the observed knee
  (starting with 50/250/500/1,000/1,500 rows/s). Report maximum sustainable rate,
  rows/core-second and source-only ceilings. Stop escalating when safety budgets
  require backpressure; overload failures remain in the report.
- Burst/catch-up tests and at least a 30-second apply pause while capture continues;
  resume with target input maintained. Record backlog peak, net drain capacity and
  recovery time. If the local budgets cannot retain the burst, exercise and report
  the bounded pressure behavior instead of overriding limits.
- Wider rows, larger tables, hot keys, inserts/updates/deletes, primary-key changes,
  supported large transactions, many tables, idle sources, source restart and
  triggered barriers. These qualify additional envelopes separately; they do not
  silently broaden the two-table headline claim.
- Pinned old/new SQL and notebook readers, catalog bindings, multiple policies and
  concurrent OLTP/query work. Measure interference and fairness; the target-load
  run without analytical queries does not promise the same rate during arbitrary SQL.
- Kill at group commit/feedback, journal read, table commit, descriptor, publication,
  prune/checkpoint and migration boundaries; repeat recovery. Test ENOSPC, corrupt
  history, lost source WAL, revocation and disk pressure without losing the prior
  coherent epoch or acknowledging missing data.

Exit: every claimed profile meets correctness, throughput, freshness and bounded
resource requirements. A passing average cannot hide a failed repeat. CPU affinity
on this host is not an EC2 simulation; actual instance scaling is later evidence.

### SP12 — Installed release and supported envelope

Build exact offline Linux x86_64 and macOS arm64 archives with the selected engine,
settings and component pins. Run inherited SY08, R04, upgrade/recovery, governed
Linux, notebook/catalog and console gates. Local Linux performance cannot be
transferred to macOS or governed profiles without measurement; record their actual
envelopes. Keep profiling optional and check the production build with profiling
fully disabled as well as the diagnostic build.

Archive installed performance results and build/inventory hashes. Update the
continuous/triggered handbook, architecture records, delivery ledger, stack map
if dependencies change, and the original synchronization plan's follow-up link.
Any product requirement/SLO expansion needs the measured hardware/workload scope;
retain historical failures and do not mark old archives retroactively qualified.

An unrelated CI blocker remains a blocker to release sign-off, with its own issue
and evidence. For example, the previously observed MinIO image pull failure is
tracked in [#92](https://github.com/supabricks/platform/issues/92); it is not evidence
for or against a synchronization optimization.

## Risk controls, rollback and issue workflow

- **Durability/replay:** failpoint tests precede performance acceptance; stop on any
  lost acknowledgment, history discontinuity or partial published transaction.
- **Resource growth:** cap actual physical sidecars, temporary files, retained
  generations and worker memory. Longer tests must exercise maintenance rather
  than passing with an empty initial store.
- **Attribution:** one mechanism per measured slice; fresh predecessor pairs;
  preserve workload, source throughput and failure denominators. Repeat after
  host contention clears, retaining invalid attempts.
- **Compatibility:** backend/journal changes need tested quiescence, ownership,
  backup and downgrade paths. Runtime rollback alone cannot undo a storage-format
  migration; restore verified compatible state or keep the newer reader available.
- **Scope growth:** optional workers/parallelism/engines need profiles that justify
  them. Stop optimizing once the target and operational headroom are established,
  while retaining experiments that showed no benefit.

Continue opening repository issues as defects are discovered. Link each confirmed
finding to the earliest failing version, reproduction, safe profile evidence,
affected invariant, planned slice and acceptance test. Update #87/#88/#91 rather
than duplicating them. Hypotheses and optional optimization proposals belong in
this plan or explicitly labeled experiment issues; do not present them as confirmed
bugs. Close an issue only after its implementation and measured acceptance exist.

Completion requires the contribution ledger, every accepted slice's comparisons,
rejected/inconclusive experiments, final sustained/recovery evidence and exact
installed qualification. A new engine, higher source input, or a single fast run
alone does not complete this performance workstream.
