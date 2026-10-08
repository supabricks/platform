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

### Controller handoff slice (in progress)

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
(1.157× decoder). No compilers were observed. Exact verification is in progress;
this still falls short of the target. The sampled observation ledger missed the
last drain publication between two reads; its final descriptor and SQL ledger
agree. The summary uses the final checkpoint as a conservative lag bound for
those last transactions. The initial summary assertion failure is retained.

### Large-profile batching slice (prepared, not yet qualified)

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
not yet a performance result. All 173 Python tests pass, including a full
65,536-operation prefix interrupted after its first table commit, replay without
rereading the journal, subsequent cross-table key move/delete, exact historical
versions, oversized-transaction rejection and duplicate-key rejection. Installed
RSS and throughput measurements against the handoff candidate remain pending.

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
