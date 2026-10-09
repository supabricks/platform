# SF100 sync scaling

Status: SF100 load-01 remains paused. Scheduling and verified-file reuse are
implemented for [#220](https://github.com/supabricks/platform/issues/220), with
local correctness checks passing. The installed, matched-prefix performance
qualification is in progress; **10× end-to-end improvement is not yet qualified**.
SP remains frozen.

## Implemented slices and current qualification

Scheduling candidate `9b81fd2` retains a maximum of 4 MiB of hashing per daemon
turn, serves IPC between turns, and immediately continues runnable verification.
A verifier parked on capture freshness uses the idle cadence instead of spinning.
Across nine paired real-daemon protocol fixtures (128/640/896 MiB, three pairs
each), median successful-publication times changed as follows:

| Inventory | Predecessor | Scheduling slice | Phase speedup |
| --- | ---: | ---: | ---: |
| 128 MiB | 0.872 s | 0.268 s | 3.25× |
| 640 MiB | 3.752 s | 0.472 s | 7.96× |
| 896 MiB | 5.205 s | 0.665 s | 7.83× |

All 36 success/corrupt-tail cases passed. Maximum observed status-request time
was 65.3 ms. These are publication protocol fixtures, not PostgreSQL-to-Delta
throughput results. [Raw measurements and package proof](tpcds-evidence/2026-10-08-eq220/scheduling-summary.json)
retain every paired attempt. Fixture setup is outside the timed publication.

Candidate `409dbd4` adds bounded process-local checksum evidence. Worker reuse
requires a live mutation lease in the same generation/identity scope; the
controller additionally requires membership in the published, durable prefix.
Each hit opens the file without following a final symlink and checks device,
inode, size, ownership, permissions, link count, mtime and ctime. Full hashing
checks metadata again after reading. An expected manifest hash must still match;
new files still receive full hashing and required fsync. Cache size is bounded
at 4,096 files and no cache is persisted. Restart, scope change, mutation,
replacement and failed requests discard or invalidate evidence. Controller
turns also limit file completions to 64 to bound metadata-only work.

This assumes the managed local filesystem reports change metadata for ordinary
writes. It is not a background bit-rot scrub: corruption that changes underlying
storage without changing any filesystem metadata is outside this cache's
detection contract. Independent reader/integrity checks and cold verification
remain available; checksum strings or filenames alone never authorize reuse.

Regression coverage includes same-size corruption with restored mtime, inode
replacement, symlinks, in-flight writes to already-read bytes, failed requests,
restart, cache bounds and required durability for new files. Installed tests
passed CLI-to-Delta-to-Sail, historical-reader restart, and >1-GiB compaction,
commit interruption/replay and exact old/new versions. The latter peaked at
483,454,976 bytes (461.06 MiB), below the unchanged 768-MiB worker ceiling.

`workload-sf100-prefix.json` freezes the first 7,385,039 generated business rows,
ending on the original COPY boundary. All 24 tables are enrolled before COPY;
the remainder stays empty. It preserves 1,024-row/4-MiB commits, the 65,536-row
publication window, large storage profile, worker bounds, eight CPUs and 16 GiB
without swap. This smaller qualification cell has explicit 80-GiB admission,
64-GiB sampled storage and 16-GiB reserve bounds. Its distinct `PREFIX_PASS` and
`PREFIX_EXACT_PASS` receipts cannot qualify the full SF100 dataset or SQL suite.
Compare both the complete prefix and the degraded-size tail; report differences
between those scopes rather than treating their speedups as interchangeable.

The first prefix fixture failed before COPY because its path exceeded the native
private-socket limit. It remains preserved as a setup failure. The short-path
`b1` attempt passed all 7,385,039 rows against installed candidate
`v0.1.0-alpha.36.eq220b`: 672.842 s loading plus 7.171 s draining, or
10,860 published rows/s. Independent typed PostgreSQL-versus-Delta comparison
passed all 24 tables (`vb1`, `PREFIX_EXACT_PASS`). This is about 2.6× the
historical whole-prefix reference, not a 10× matched throughput result. The resulting terminal-error reporting problem is tracked
separately in [#221](https://github.com/supabricks/platform/issues/221).

### Decoder allocation slice

Candidate `dfa654f` removes temporary byte slices and repeated numeric-format
work in strict pgoutput tuple decoding. Type validation, transaction boundaries,
NULL/TOAST handling and row/value limits remain unchanged. All 170 Python tests
passed. Three alternating predecessor/candidate process pairs on the same
cloned paused journal produced nine unprofiled planning times per candidate:
median 0.682193 s before and 0.515655 s after (24.4% less planning time).
Each plans the same 16,384-row prefix. This is an isolated planning measurement;
its separate cProfile timings include profiler overhead and are not throughput.
The installed `c1` prefix trial passed: 609.269 s loading plus 4.729 s draining,
or 12,028 rows/s overall (1.108× the cache slice). Over the fixed 6.5–7.3-million
row cohort, rounded up to actual publication boundaries, throughput changed
from 8,049 to 8,482 rows/s (1.054×). No Cargo/rustc processes were observed in
either timed run. Both respected the 65,536-row backlog window. Exact typed
verification of `c1` passed all 24 tables (`PREFIX_EXACT_PASS`). The 20k/10× target remains unqualified.

### Controller handoff slice

Completed apply receipts and durable publication commits can wake the existing
sync state machine without waiting for general 200-ms maintenance. The readiness
probe is only a hint: capture observation, process ownership and RSS/recycling,
policy fencing, mailbox completion and run deadlines still gate advancement.
General maintenance and all admission limits remain unchanged. Candidate `1402769` is packaged as `v0.1.0-alpha.36.eq220d`; 231 Rust library
tests and all 14 publication protocol tests passed. Installed continuous and triggered suites passed atomic groups, idle stability,
fixed barriers, complete-transaction admission, pause/resume, capture SIGKILL,
daemon restart, schema drift/resync and independent pinned historical readers.
The `d1` prefix passed in 512.155 s loading plus 5.223 s draining: 14,274 rows/s
overall (1.187× decoder), with 9,815 rows/s over the degraded-scale cohort
(1.157× decoder). No compilers were observed. Exact verification passed all 24 tables;
this still falls short of the target. The sampled observation ledger missed the
last drain publication between two reads; its final descriptor and SQL ledger
agree. The summary uses the final checkpoint as a conservative lag bound for
those last transactions. The initial summary assertion failure is retained.

### Large-profile batching slice

The full-worker diagnostic on a private reconstruction of the paused published
prefix still spends time in planning, table preparation and inventory metadata
walks after checksum reuse. Its cProfile run is diagnostic only (2.268 s with
profiler overhead, 376,270,848-byte peak RSS); it is not an unprofiled throughput
measurement and does not publish or acknowledge anything in the paused cell.

A separate candidate raises the explicit `large` profile's aggregate apply
ceiling from 16,384 to 65,536 row operations. The `compact` profile and the strict
16,384-row ceiling on a *single source transaction* remain unchanged. Complete
transactions fill the admitted prefix; a transaction is never split. The journal
read still stops at 16 MiB, Arrow values at 32 MiB, the sealed plan at 64 MiB and
the worker at 768 MiB / 300 seconds. COPY size, 65,536-row publication window,
CPU/RAM and SQL timeout remain unchanged. This is an explicit admission change,
measured separately below. All 173 Python tests pass, including a full
65,536-operation prefix interrupted after its first table commit, replay without
rereading the journal, subsequent cross-table key move/delete, exact historical
versions, oversized-transaction rejection and duplicate-key rejection. Installed
`eq220e` (source `05f54e1`) applied 57,344 paused-journal rows at 507,432,960-byte
peak RSS in the diagnostic probe. Its >1-GiB compaction/interrupted-commit/replay
fixture passed exact old/new versions and peaked at 510,029,824 bytes. Both are
below 768 MiB. The `e1` prefix passed at 17,575 rows/s overall (1.231× handoff),
and 15,497 rows/s over the degraded-scale cohort (1.579× handoff), with no observed
compilers and the same 65,536-row backlog window. Exact prefix verification passed all 24 tables. Neither throughput target is
qualified yet.

### Scalar allocation slice

The large-batch profile still allocates many Decimal digit tuples while checking
precision/scale and rebuilds key metadata for every row. A follow-up caches only
bounded, immutable numeric type shapes, uses exact exponent/adjusted predicates
for the common declared-scale case, and retains the original tuple-based fallback
for other exponents. It also reuses primary-key metadata within one transaction,
validates unchanged-TOAST markers during insert tuple decoding, and avoids
constructing the same key twice when no old-key tuple exists. All 175 Python tests passed, including low-context-precision and signed-zero/
exponent boundaries; all 38 harness tests passed. Across three alternating process pairs (nine unprofiled samples per candidate),
median planning changed from 1.491613 to 1.176265 s (21.1% less time); every
57,344-row sealed plan had the same SHA-256. Installed `eq220f` / `0bc57c1` passed
the full prefix at 17,754 rows/s and the late cohort at 16,512 rows/s. These are
only 1.010× and 1.066× the batching candidate: the isolated planning improvement
did not translate proportionally to total throughput. Exact verification passed
all 24 tables. The 20k/10× target remains unqualified. No decimal rounding or float conversion is introduced.

The additional `sf100-growing-prefix` profile admits 14,770,127 source rows,
rounded to a complete COPY boundary: more than twice the paused prefix. It uses
the same 8-core/16-GiB/no-swap container, COPY size, backlog, worker and storage
bounds. Its receipt/hash cannot substitute for the shorter prefix or full SF100.
This larger qualification has not started; the original SF100 cell remains paused.

### Capture tuple cursor slice

The source capture parser still created marker/length slices for every field.
It now uses a bounded local cursor, validates every length and UTF-8 field before
journaling, and retains schema/transaction/framing checks. All 176 Python tests
pass, including truncation at every byte, invalid lengths/kinds/UTF-8 and exact
reader boundaries. Three alternating process pairs on the same 57,344-row cloned-journal range
reproduce all encoded transactions exactly. Median decoding changes from
0.431532 to 0.239266 s (1.804×); this excludes sockets, durability and publication.
Installed `eq220g` / `7727538` passes the prefix at 18,546 rows/s overall (1.045×
scalar) and 16,513 rows/s in the late cohort (effectively unchanged). Exact
verification passed all 24 tables. Fixed-label process counters suggest both
capture and apply remain substantial consumers; sampled late-cohort averages
are about 0.61 capture cores, 0.68 apply cores and 0.22 controller cores. These
are diagnostic samples and may miss short process tails. The observer records
no argv or SQL and introduces no quiet gate. Neither target is qualified.

### Bounded wire read-ahead

The capture transport previously waited for and read every packet header/body
separately. A bounded 64-KiB receive now serves buffered complete packets in
order, keeping the existing `MAX_MESSAGE + 30` total frame-buffer ceiling and
partial-frame deadlines. Consumed bytes are compacted only before another
receive, avoiding per-packet buffer shifts. Every header, payload, UTF-8 tuple,
transaction and durable acknowledgment still follows the existing validation.
All 179 Python tests passed, including coalesced packets, partial tails, invalid
buffered headers and the maximum-frame memory bound. Installed continuous and
triggered suites pass pause/resume, forced capture/daemon restart, schema drift,
fixed barriers, complete-transaction admission and pinned historical readers.
Three alternating process pairs on the same cloned journal reproduce every
encoded transaction exactly. Median socketpair transport plus strict decoding
changes from 0.569073 to 0.277381 s (2.052×). This excludes PostgreSQL, durable
journaling and publication. Installed `eq220h` / `ea7690d` passes the prefix at 20,786 rows/s overall
(1.121× capture-cursor) and 17,156 rows/s in the late cohort (1.039×). Exact typed
verification passes all 24 tables. The overall rate crosses 20k, but the late
cohort still misses the target. No compilers were observed during the timed run.
The late cohort averages 2.15 of eight allocated CPU cores, including about 0.51
capture and 0.66 apply cores. Across the load, sampled apply RSS peaks at 431 MiB
and capture at 111 MiB; full-cell anonymous memory peaks at 1.78 GiB. Total
cgroup memory reaches its 16-GiB ceiling primarily through filesystem cache.
The existing 768-MiB worker bound is not exhausted. These samples do not prove
that synchronous filesystem latency is absent. A separate coarse diagnostic
package is profiling control reads, worker phases and publication; its timings
will not substitute for an uninstrumented qualification.

The first coarse diagnostic attempt `p1` completed its load but is excluded from
capture attribution and acceptance: inherited opt-in capture/export hooks were
installed twice. All raw traces and its invalid-instrumentation disposition are
retained. [#224](https://github.com/supabricks/platform/issues/224) fixes package
hook insertion and runtime installation to be idempotent; the regression suite
passes eight tests, including rebuilding a diagnostic overlay from another one.
The corrected `p2` attempt completed with a single instrumentation layer. In its
late cohort, 27 complete apply runs had median 1.266 s total worker time and
1.190 CPU-seconds per request. Nested planning took 586 ms, table application
330 ms, inventory 71 ms and previous-inventory verification 46 ms. The median
manifest-to-worker-end segment was 78 ms; another 246 ms elapsed before durable
publication, including GC and controller work. These medians are not additive.
Over its approximately 45-second capture snapshot window, control reads cost
16.266 s and native fsync calls totaled 14.532 s across threads. The latter also
occurs within other stage measurements. `p2` throughput includes instrumentation
and is not acceptance or a reliable fixed overhead correction: it measured
19,923 rows/s overall and 18,001 in the late cohort versus uninstrumented H's
20,786 / 17,156. Retain both observed scopes without inferring a speedup.

### Capture control-version reuse (prepared)

Capture currently reopens and reparses its daemon-owned control JSON for every
replication message. The next slice retains just one parsed control version,
bounded by the existing 64-KiB limit. Every loop still checks inode, ownership/
permission metadata, link count, size, mtime and ctime before reusing it. A changed
version is opened without following a symlink, read with a fixed bound, and
checked against both the open descriptor and current path before being cached.
Atomic replacement, in-place changes, errors and process restart invalidate the
value. Rapid replacement retries are bounded and then report unavailability.
Desired state, generation fencing and owner health retain their per-loop checks;
journal-reader authorization still performs independent uncached validation.
No acknowledgment, durability, queue, CPU or memory limit changes. All 184 Python
tests pass, including warm reuse, restored-mtime writes, mid-read replacement,
symlinks/hardlinks, oversized controls, error/restart invalidation, bounded churn
and existing pause/fencing/feedback tests. Installed continuous and triggered
suites pass. A >1-GiB compaction/interrupted-commit/replay fixture passes exact
historical/current versions at 507,518,976-byte peak RSS (484.0 MiB).
Three alternating predecessor/candidate process pairs poll identical control
bytes 100,000 times per sample. Median time changes from 1.784017 to 0.333404 s
(5.351×), with identical parsed contents. This is a polling component probe,
not a published-row rate. Installed `eq220i` / `bfc87c7` failed the uninstrumented `i1` prefix at 4,693,815
committed and 4,628,279 published rows with `wal_budget`. This is not a throughput
pass; every receipt is retained. [#225](https://github.com/supabricks/platform/issues/225)
tracks the newly exposed source-retention blocker.

### WAL restart-snapshot pressure (prepared)

The slot's restart LSN, reconstructed from source LSN minus retained bytes,
advances in approximately 15-second steps in `i1`, while durable feedback
continues advancing. Its last restart point stays at `0/44B2B360` for about
15 seconds; the last successful observation has 419,522,120 retained bytes,
just below the subsequent failing 80%-of-512-MiB check. H peaked at
366,617,752 bytes. PostgreSQL's [background writer](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/postmaster/bgwriter.c)
logs running-transaction snapshots every 15 seconds; logical decoding uses
those records to propose safe restart progress. This cadence is consistent
with the observed retention steps, not evidence of missing durable feedback.

A new slice requests [`pg_log_standby_snapshot()`](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)
after another quarter-budget of WAL under pressure, at most once per second.
Idle pressure without further WAL growth does not generate repeated requests.
The operation logs transaction state; it does not advance the slot, skip WAL,
or acknowledge data. PostgreSQL's oldest-transaction fence, durable capture
feedback, the existing 80% stop and server-enforced WAL cap remain unchanged.
Source progress now includes confirmed/restart/source positions and request
counts. A bounded installed fixture will verify progress, long-transaction
pinning and exact application under the existing 32-MiB minimum WAL profile.
All 187 Python tests and 38 qualification-harness tests pass. Installed `eq220j`
passes the pressure fixture: 3,700 rows apply exactly; a live long transaction
keeps its restart pin despite snapshot requests, and the pin advances only after
commit and durable feedback. The 32-MiB WAL limit is unchanged. The first fixture
attempt failed before inserting business rows because of ambiguous SQL parameter
types; its failure and the corrected second attempt are both retained. Continuous,
triggered and automatic-rollover checks pass. The installed maintenance suite
also preserves pinned Sail history, prunes and restarts the journal without gaps,
collects only unpinned old roots, restores a compacted epoch from stopped backup
with capture fenced, and retires source resources without deleting retained
history. The uninstrumented `j1` prefix passed at 25,944 published rows/s
overall and 20,635 in the fixed late cohort (283.963 s loading plus 0.692 s
draining). Independent typed verification passes all 24 tables. No compilers were observed during the timed load. The sampled commit-to-publication upper-bound p95 is 3.164 s.
These results precede any preparation pipeline change. I failed, so the H-to-J
comparison combines control-version reuse and restart-snapshot maintenance; it
cannot attribute the gain to either change alone. The fresh original baseline
and growing-prefix qualifications remain outstanding; 10x is not established.

### Preparation pipeline: first slice

The first slice separates strict pgoutput decoding/type conversion and complete-
transaction selection from planning against a pinned Delta version. In the
explicit large profile, one request-local preparation thread overlaps that CPU
work with initialization and previous-inventory verification. State-dependent
key lookup, TOAST resolution, conflict detection, Delta writes, verification and
publication retain their order. Compact applies remain synchronous. Bootstrap
and sealed-plan replay create no preparation job or additional journal read.

The existing owner service still authorizes one exact LSN range, checks its
identity and returns a complete fenced response before preparation starts.
There is no speculative next-range read, extra journal snapshot, queue, process,
WAL acknowledgment or publication authority. Both stages share the unchanged
768-MiB process RSS ceiling and original deadline. Complete transactions retain
the 16,384-row/4-MiB limits, with the existing 65,536-row aggregate apply bound.
A failure cancels and joins preparation before returning to the reusable worker;
decoded data is ephemeral and never reused across requests. Cancellation and
deadline checks occur between bounded transactions. Read-only planning remains
protected by its mutation lease and inventory boundary after preparation joins.

This thread can overlap Python work with native calls and filesystem operations
that release the interpreter lock. It does not provide parallel execution of
two Python CPU stages. Deterministic concurrency tests require verification to
run while preparation is in progress, compare sealed plans byte-for-byte, and
check failure propagation, cancellation, deadline expiry and single consumption.
The retained J prefix is the before measurement. Three alternating J/K process
pairs (nine warm samples each) verify and plan the same cloned 57,344-row range.
Median time is 1.255865 s before and 1.237611 s with preparation, a 1.015x ratio.
Every sealed plan has the same SHA256. This small component difference excludes
journal transport, initialization, Delta mutation and publication; it does not
establish an end-to-end gain. The full-prefix comparison is pending.
The corrected Python suite passes 195 tests; a separate eight-test reuse suite
includes the added large-profile multi-epoch/restart case. All 38 harness and
three evidence-inventory tests pass. Initial fixture failures and corrections
are retained. Installed continuous and triggered checks explicitly select the large profile
and pass. Applying that adapter to the manual snapshot/full maintenance fixture
correctly rejected policy creation; the failed setup and cleanup are retained.
The adapter now accepts only supported continuous/triggered suites. The original
compact maintenance suite is running separately. Ten focused preparation tests
pass, including large-profile compaction with exact history and interruption at
compaction/apply boundaries; no production policy guard was relaxed.

Cross-batch look-ahead remains a later slice. It needs a separately authorized,
fixed read-only LSN range, one queued batch with aggregate memory admission,
identity/schema/policy fencing on consumption, and discard-on-restart recovery.
Only decoding may run ahead of the previous publication; any Delta-dependent
plan must bind to the actual committed predecessor. A process or native decode
implementation would be needed to parallelize two Python CPU stages across
cores. Evaluate that after this measured boundary, without raising limits.

A separate source-review opportunity for repeated ownership-table scans during
historical cleanup is tracked in [#223](https://github.com/supabricks/platform/issues/223).
Its prepared lookup patch is not part of these candidates; the current evidence
does not isolate its cost from other controller work.

## Preserved baseline

The user requested at least an order-of-magnitude sync improvement before
continuing the large load. Container `eq03-sf100-load-01` is frozen with
7,385,039 committed and 7,319,503 published rows. Its last worker-written result
still says RUNNING because the process is paused; the separate
[pause receipt](tpcds-evidence/2026-10-08-sf100-load-01/pause.json) records the
actual disposition and hashes of the unmodified result and ledgers.

Docker pause freezes processes, not wall-clock deadlines. Do not blindly unpause
this cell: outstanding worker/capture deadlines and the supervisor timeout may
expire while frozen. Reconcile source acknowledgments, the published LSN and
outstanding operations before selecting recovery or a fresh measured attempt.
Preserve this attempt and never automatically repeat ambiguous COPY commits.

## Measured bottleneck

The loader spent 1,642 of 1,774 active seconds waiting for publication (92.6%).
Recent publication rates were approximately 2,000 rows/s. All 514 completed
incremental publication receipts are summarized by the read-only
`e2e/tpcds/sync_costs.py` extractor in
[batch-costs.json](tpcds-evidence/2026-10-08-sf100-load-01/batch-costs.json).

| Median over batches | First 30 | Last 30 |
| --- | ---: | ---: |
| Worker start to manifest | 465.5 ms | 1,914.5 ms |
| Manifest to publication | 369.5 ms | 5,218.5 ms |
| Worker start to publication | 829 ms | 7,192 ms |
| Journal read, nested within worker | 19 ms | 28 ms |
| Delta-reported append execution, nested within worker | 8 ms | 10 ms |
| Rows appended per batch | 16,384 | 16,384 |
| Entire inventoried prefix | 14.45 MB | 642.38 MB |
| New Parquet payload | 1.32 MB | 1.52 MB |

Intervals come from persisted wall-clock timestamps. Medians do not sum exactly;
nested metrics must not be added to the containing phase. Different tables are
loaded in the two cohorts, so this is diagnostic evidence, not a controlled
before/after comparison. The inherited bootstrap `elapsed_seconds` field in
each manifest is not that incremental batch's runtime.

The worker's `incremental/storage.py::verify_previous` hashes the preceding
inventory, and `inventory` hashes the resulting inventory again. The controller
then verifies every byte before publication. The #186 scheduling correction is
already present: `analytics.rs::Verifier::advance` processes at most 4 MiB per
turn, with a 20-ms continuation delay in `daemon.rs`. At approximately 642 MB,
that alone requires over 150 turns. The observed manifest-to-publication phase
also includes other controller work; it is not all SHA-256 CPU time.

A separate read-only installed-worker probe over the paused 662,928,071-byte,
1,076-file prefix measured about 0.48 s for `verify_previous` and 0.51 s for
`inventory`, across three hot-cache repetitions. The original container stayed
paused and was mounted read-only in the probe. Its
[receipt](tpcds-evidence/2026-10-08-sf100-load-01/hash-cost.json) and script are
retained alongside the baseline. This confirms material repeated hashing cost;
it is not an end-to-end speedup measurement. Each pass reads roughly 400 times
the typical new batch payload. This is verification read amplification, not
Parquet rewrite amplification.

## Required outcome and measured slices

Require at least **20,000 published rows/s and at least 10× the matched baseline**
on the same dataset, table mix, resource limits and input/acknowledgment window.
Faster COPY with increasing backlog does not qualify. Report throughput,
source-commit-to-publication lag, CPU, RSS, read/write bytes, storage growth,
compaction and exact correctness. The target applies as the live set grows,
not only to the initial empty database. Qualification should cover both the
baseline's degraded scale and larger inventories/rollover.

1. **Freeze the diagnostic benchmark.** Build reproducible small, paused-scale
   and larger live sets with the same 24-table manifest. Instrument capture,
   planning, worker verification/inventory, append, durability and controller
   verification/publication. Keep profiler overhead controls. Separate active
   CPU/IO time from scheduler wait; bind source/package identities and inputs.
2. **Remove verification scheduling waste.** Qualify bounded asynchronous or
   cooperatively scheduled verification that does not sleep once per 4-MiB
   chunk. Keep IPC responsiveness, cancellation, capture freshness and bounded
   work/memory. Continue hashing every byte in this slice to measure its isolated
   contribution. This slice alone cannot promise 10× while worker work remains.
3. **Reuse established immutable-file verification safely.** Design an explicit
   authority for files already verified and published in the same installation,
   capture and storage generation. Avoid repeated worker/controller hashing of
   unchanged files, while hashing new files fully. Qualify mutation/replacement
   detection, identity and ownership fencing, restart invalidation, compaction,
   corruption rejection and fallback to full verification. A copied checksum or
   matching pathname alone is not proof. Preserve fsync-before-publication and
   independent historical readers.
4. **Address the next measured bottleneck.** Measure remaining Python planning,
   capture/journal costs, table scheduling and batch limits. Change one logical
   contributor at a time. Larger batches or parallelism require independent
   admission and exact transaction/replay tests; they are not substitutes for
   removing live-set-dependent work.
5. **Qualify the installed path, then SF100.** Run matched predecessor/candidate
   measurements after every logical slice, including correctness and resource
   limits. Verify sustained growing-data throughput, rollover/compaction, restart,
   corruption and historical-reader behavior. Resume large-scale qualification
   only after a recovery disposition for the frozen attempt is documented.

For every slice retain raw attempts, fixed input and resource profiles, phase
timings, total throughput/lag and exact data checks. Report both the marginal
change from its predecessor and the cumulative change from this baseline.
Do not add improvements from overlapping phase measurements or accept a speedup
that weakens correctness, durability, memory/storage bounds or responsiveness.
