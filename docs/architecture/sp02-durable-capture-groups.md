# SP02: bounded durable capture groups

SP02 follows the merged [SP01 journal recovery slice](sp01-journal-contention-recovery.md).
It isolates capture batching: the spool still uses SQLite DELETE journaling and
`synchronous=FULL`. PostgreSQL, Delta, the publication catalog, source clients,
resource envelope and sync modes remain unchanged. This diagnostic experiment is
not a signed release qualification or a new supported throughput claim.

## Durable write and feedback behavior

`Spool.append_many` validates the entire bounded, ordered group before beginning
its write transaction. Replayed records must retain their commit LSN and payload
checksum; new records retain individual transaction identities and predecessor
links. The group inserts those records and updates the captured cursor, byte total
and barrier metadata in one SQLite commit. Neither a bad later member nor a full
disk can expose a successful prefix of that group.

The receive loop accumulates complete transactions outside SQLite write
transactions. It flushes at 32 transactions, 1 MiB of payload, or 10 ms since the
oldest complete pending transaction, whichever comes first. A supported transaction
above the byte target gets a dedicated group, within the existing 4 MiB transaction
limit. The decoder's partial transaction and complete pending group have separate
bounds. Physical reservations additionally account for database pages, indexes and
the rollback journal; the accumulation target is not a disk reservation.

Transactional barriers close their groups promptly. The loop also flushes before
blocking maintenance/status operations, pause, deletion and orderly SIGTERM/SIGINT
shutdown. Generation fencing discards unacknowledged buffered work. The existing
supervisor's hard-stop fence remains a process kill. Partial source transactions
are never committed. Source loss restarts from the durable cursor.

Incremental wire framing retains a bounded partial packet across socket deadlines.
The former 200 ms poll and three-second whole-packet read cannot override a shorter
pending-group wait. The age target bounds accumulation; it does not cap a blocked
durable commit or promise real-time scheduling.

Keepalives and group completion acknowledge only the cursor read from durable
SQLite, never decoded progress or the sender's WAL end. If COMMIT reports an
ambiguous error after leaving its transaction, the writer closes and reopens the
database, verifies its complete chain and metadata, and checks the proposed group
before treating it as durable. Otherwise it returns a failure without feedback.

## Component screen and setting selection

The predeclared screen ran 33 randomized ten-second trials, three per setting,
with fresh spools and seed 20260926. All passed independent stopped-file validation
of retained payloads, checksums, predecessor links and the pruned-prefix count.
No trial overlapped detected external builds. Each trial used real durable writes,
periodic authorized prefix pruning, and either a bounded two-millisecond reader
or the explicitly named reader-disabled control.

| Setting | Offered transactions/s | Reader | Median completed transactions/s | Mean transactions/group (trial median) | Native syncs/transaction (trial median) |
| --- | ---: | --- | ---: | ---: | ---: |
| Grouping disabled, candidate code | Saturated | On | 52.58 | 1.00 | 4.0152 |
| Count 8, age 10 ms | Saturated | On | 395.39 | 8.00 | 0.5283 |
| **Count 32, age 10 ms** | Saturated | On | 1320.06 | 32.00 | 0.1553 |
| Count 128, age 10 ms | Saturated | On | 3209.15 | 128.00 | 0.0623 |
| Count 32, age 0 ms | 500 | On | 52.50 | 1.00 | 4.0152 |
| Count 32, age 5 ms | 500 | On | 499.09 | 13.01 | 0.3363 |
| **Count 32, age 10 ms** | 500 | On | 499.13 | 15.15 | 0.2928 |
| Count 32, age 25 ms | 500 | On | 499.07 | 21.83 | 0.2120 |
| Count 32, age 10 ms | 25 | On | 25.00 | 1.00 | 4.0000 |
| Count 32, age 10 ms | Saturated | Off | 1337.43 | 32.00 | 0.1554 |
| Count 32, age 10 ms | 500 | Off | 499.15 | 15.10 | 0.2937 |

The selection rule retained the proposed 32/1 MiB/10 ms setting if its saturated
median exceeded 750 transactions/s and its paced 500-transaction/s median exceeded
475, with correctness intact. Both conditions passed. Count 8 missed the component
capacity target; count 128 provided more saturated capacity than this slice needed.
Five milliseconds used more syncs per transaction for the same paced throughput;
25 milliseconds reduced sync work but extends the accumulation allowance. These
are screening observations, not tuned production performance guarantees.

The grouping-disabled comparison is an ablation within candidate code, not an
SP01 predecessor measurement. The synthetic 103-byte transactions contain two small row events; actual source
transactions can differ in width. Their
rates exclude PostgreSQL, decoding, Delta and publication. Native sync
totals include pruning. The reader-disabled trials quantify this component reader's
cost and still run independent final correctness checks; they do not establish the
cost of the complete end-to-end observer. All individual results and ranges are in
the [component archive](sync-performance-evidence/2026-09-26-sp02/component-screen/README.md).

## Profiler activation controls

Before the full matrix, each final diagnostic package ran three profiling-off/on
pairs at 4 CPUs and 50 changed rows/s. All twelve trials completed correctly and
passed the five-second p95 gate, without detected external-build overlap.

| Unchanged runtime | Off p95 median (range), ms | On p95 median (range), ms | Difference of median CPU, cores | Difference of median peak memory |
| --- | ---: | ---: | ---: | ---: |
| SP01 with common probe | 3,747.799 (3,677.948–3,784.414) | 3,670.593 (3,631.044–3,701.840) | +0.036 (+4.75%) | −0.64% |
| SP02 with common probe | 3,667.487 (3,612.205–3,710.030) | 3,665.905 (3,583.688–3,695.581) | +0.023 (+2.93%) | +3.26% |

These are three-trial screening controls, not latency-equivalence tests. The small
negative latency differences do not establish a profiler speedup. Both arms retain
the same imported code; activation controls measure enabled profiling work.
Individual results, ranges and paired deltas are retained for
[SP01](sync-performance-evidence/2026-09-26-sp02/predecessor-controls/comparison.json)
and [SP02](sync-performance-evidence/2026-09-26-sp02/candidate-controls/comparison.json).
The predeclared overload controls then ran three off/on pairs at each of 8 and
16 CPUs, all on the unchanged SP02 package. All twelve completed with correctness
and five-second p95 freshness, without detected contention.

| CPUs / offered rows/s | Off p95 median (range), ms | On p95 median (range), ms | Median paired CPU change | Median paired peak-memory change |
| --- | ---: | ---: | ---: | ---: |
| 8 / 1,000 | 4,549.422 (4,482.542–4,604.497) | 4,554.581 (4,545.246–4,687.250) | +2.69% | +5.55% |
| 16 / 1,000 | 4,589.127 (4,499.822–4,615.028) | 4,521.714 (4,434.839–4,639.301) | +4.34% | −0.37% |

Paired median source-rate changes are −0.57%/−0.16%; paired median p95 changes are
+1.40%/−1.47%. This measured activation cost is small relative to the capture-stage
gain, but should remain visible in resource comparisons. [All overload controls](sync-performance-evidence/2026-09-26-sp02/overload-controls/comparison.json)
retain individual outcomes and ranges. No latency-equivalence claim is made.

## Instrumentation and qualification

Both diagnostic arms inherit the byte-identical SP01 binary and dependencies and
use the same updated probe. The candidate changes the capture spool, accumulator,
wire framing and worker; its checked-hash bytecode is rebuilt with relative file
names. Package overlays verify the predecessor remains unchanged and both installed
inventories validate.

The probe measures grouped `executemany` inserts and retains the existing append
span label. Durable transaction and byte counters exclude replay-only records;
group counts exclude replay-only groups. SQLite COMMIT/native-sync totals also
include metadata and pruning. New per-transaction metrics divide those totals by
committed source transactions, not by groups. Group accumulation and durable commit
elapsed time are separate. Snapshot gauges distinguish decoded, durable and
feedback cursors; absent gauges in older archives remain absent.

Socket-wait spans now nest inside deadline-aware receive. Their inclusive totals
must not be added or mistaken for protocol-processing self time. Profiler activation
controls use unchanged packages and identical harnesses before attributing runtime
changes. Component controls include the native counter probe in all arms.

Local validation: 72 analytics tests, 39 benchmark-accounting/observer tests and three
release-evidence tests pass. All 46 final check runs on frozen runtime `838f0b1` pass
after the retained macOS retry described below. Added fault coverage includes process death before the
group transaction, before/after commit and before/after feedback; mixed replay/new
records and changed duplicates; pruning/reconnect anchors; barriers within atomic
groups; actual SQLite FULL rollback; ambiguous committed/uncommitted responses;
partial framing deadlines; oversized partial transactions; keepalives, pause,
orderly signals and generation fencing. Process-crash tests do not qualify power
loss or machine failure.


Qualification caught a race in the first candidate: the accumulation deadline
could expire between `before` and `add`, and the second age check treated that as
a hard capacity error. The corrected `add` rejects only hard count/byte overflow;
new expiration requests an immediate flush. A deterministic regression test fails
on the original implementation and passes on the corrected version. The retained
failed spool passed read-only verification, with last observed feedback equal to
its durable cursor.

The observer now preserves durable capture failures when worker status is replaced.
Healthy-path SQL is unchanged. Only transient status-file absence is counted and
bounded; ten consecutive omissions or more than 100 total stop measurement. Other
missing files, invalid JSON, lost transaction markers and cleanup failures remain
errors. [Issues #104](https://github.com/supabricks/platform/issues/104) and
[#105](https://github.com/supabricks/platform/issues/105) retain the diagnoses.
The [superseded archive](sync-performance-evidence/2026-09-26-sp02/superseded/README.md)
keeps the earlier 33 component trials, 12 controls and incomplete first comparison
separate from final acceptance. No failed attempt becomes a passing latency.

## Full-stack comparison

The frozen matrix ran twelve seeded pairs: three repeats each at 4/16 logical CPUs and 50 offered changed rows/s, and 8/16 CPUs at 1,000. Four source clients changed one row in each of two 10,000-row tables per transaction. Each fresh fixture retained the five-second source baseline, five-second warmup and catch-up, 45-second load, 120-second drain, complete SMT pairs and 16 GiB/no-swap/no-quota envelope. All 24 trials had clean teardown; no detected build contention required replacement. This remains a shared desktop with sampled contention detection, not exclusive hardware or EC2 equivalence.

SP02 completed **12/12** with full frozen-source equality and five-second p95 freshness. SP01 completed all six low-load trials; all six overload trials hit `publication_drain_timeout`. The stopped predecessor spools retain a lower bound of **8,448–8,583 uncaptured source commits** after the drain timeout, independently confirming unfinished capture. Those failures have no complete latency percentile. The change from failure to completion is reported separately from capture throughput; it is not an end-to-end percentage speedup.

| CPUs / offered rows/s | SP01 complete | SP02 complete | SP01 p95 median (range), ms | SP02 p95 median (range), ms | SP02 generated rows/s median (range) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 4 / 50 | 3/3 | 3/3 | 3,697.001 (3,644.657–3,711.271) | 3,697.623 (3,612.900–3,703.642) | 50.038 (50.035–50.038) |
| 8 / 1000 | 0/3 | 3/3 | Unavailable | 4,568.230 (4,506.046–4,632.884) | 685.612 (685.211–689.444) |
| 16 / 50 | 3/3 | 3/3 | 3,718.995 (3,661.811–4,139.538) | 3,664.476 (3,635.824–3,695.437) | 50.037 (50.036–50.039) |
| 16 / 1000 | 0/3 | 3/3 | Unavailable | 4,596.328 (4,492.201–4,605.376) | 686.445 (683.580–686.575) |

All individual results, ranges, paired deltas and comparisons with the immutable original profiling baseline are in the [machine-readable comparison](sync-performance-evidence/2026-09-26-sp02/main/comparison.json). The fresh predecessor supports attribution; historical differences are contextual, not controlled causal effects. Three repetitions are screening evidence, not statistical certainty. There is no established low-load latency speedup or guarantee.

### Capture contribution

| CPUs / offered rows/s | Captured transactions/s, SP01 → SP02 | Transactions/group, SP02 | Sync calls/transaction, SP01 → SP02 | SQLite durable ms/transaction, SP01 → SP02 |
| --- | ---: | ---: | ---: | ---: |
| 4 / 50 | 25.86 → 25.88 | 1.07 | 4.0141 → 3.7577 | 27.654 → 22.397 |
| 8 / 1000 | 30.11 → 343.46 | 15.71 | 4.0121 → 0.2657 | 29.948 → 1.946 |
| 16 / 50 | 25.86 → 25.88 | 1.07 | 4.0141 → 3.7542 | 27.759 → 22.270 |
| 16 / 1000 | 29.56 → 343.60 | 15.97 | 4.0123 → 0.2616 | 30.558 → 1.947 |

Under overload, the median paired reduction is **93.38% / 93.48% in sync calls** and **93.50% / 93.63% in SQLite durable time per transaction** at 8/16 CPUs. Capture advances roughly **11.4× / 11.6×** as many transactions per second in the sampled load windows. Individual commits still cost about 29–30 ms and four sync calls: the gain comes from sharing that durable cost across complete transactions. These counts include control transactions; they are not source changed-row throughput.

At overload the median groups contain 15.71/15.97 transactions and 1,898/1,930 payload bytes, advancing about 41.5 kB/s versus 3.5–3.6 kB/s previously. At low load groups contain only 1.07 transactions. All-run mean accumulation is about 9.3–9.7 ms; the largest observed accumulation is 14.05 ms. The 10 ms setting is a scheduling deadline, not a real-time guarantee or a cap on durable I/O. The decoder and complete-transaction bounds remain independent.

Across 2,372 comparable cursor snapshots, feedback never exceeds the observed durable cursor. This sampled check supplements the fault tests; it is not a proof that sampling sees every feedback event. [Profile analysis](sync-performance-evidence/2026-09-26-sp02/profile-analysis.json) retains decoded/durable/feedback observations, group ages/bytes, capture CPU and native sync time.

### Resource cost and remaining work

| CPUs / offered rows/s | Mean CPU cores, SP01 → SP02 (trial medians) | Median paired CPU change | Peak cgroup memory MiB, SP01 → SP02 (trial medians) | Median paired peak-memory change |
| --- | ---: | ---: | ---: | ---: |
| 4 / 50 | 0.787 → 0.816 | +3.67% | 1144.1 → 1154.1 | +0.08% |
| 8 / 1000 | 1.189 → 1.283 | +7.91% | 1372.5 → 1303.6 | -3.73% |
| 16 / 50 | 1.232 → 1.283 | +4.14% | 1261.8 → 1286.3 | +3.13% |
| 16 / 1000 | 1.365 → 1.506 | +10.92% | 1360.4 → 1361.9 | +0.87% |

The 16-core overload CPU increase crosses the 10% investigation threshold. SP02 actually captures and applies the generated workload, whereas SP01 remains behind. In comparable capture snapshot windows, CPU rises from about 1.09 to 4.59 seconds while captured transactions rise more than elevenfold. Successful apply workers started during load also handle far more journal transactions; their retained lifecycle CPU counters explain additional useful work but cross window edges and exclude failed workers, so they must not be added to cgroup load CPU. No equal-work CPU-efficiency claim follows from comparing a completed pipeline with a stalled one. All low-load pairs pass freshness and correctness; their paired median CPU cost is 3.67–4.14%, with no material memory regression observed.

The source-only candidate baselines generate 848–877 changed rows/s, and the full-stack source generates 684–689, **below 1,000**. SQL COMMIT accounts for 96.76–97.08% of measured source SQL time. Its p50 is 10.73–10.88 ms and p95 18.17–18.45 ms. Increasing affinity from 8 to 16 logical CPUs does not materially raise throughput. [Issue #107](https://github.com/supabricks/platform/issues/107) tracks source qualification under SP06; this slice does not change client count or weaken durability.

Publication remains the larger latency component after capture catches up. Candidate overload trial-median stage p95s are approximately 0.27 s to observed capture, 2.57–2.59 s before admission, 0.11 s dispatch, 1.55 s worker preparation and 0.49–0.56 s publication. These are overlapping marginal percentiles, not additive pieces of end-to-end p95. Successful apply workers still spend about 712–716 ms traversing generation boundaries versus 19–21 ms in Delta merge; [issue #91](https://github.com/supabricks/platform/issues/91) and SP04 address that work. Neither this 45-second matrix nor the component screen establishes sustained hour-long capacity or power-loss qualification.

## macOS release qualification

The first [installed macOS sync job](https://github.com/supabricks/platform/actions/runs/36207733744/job/108312300330)
passed triggered sync but failed the unchanged continuous burst freshness gate:
200 changed rows took **5,290.978 ms** to publish. Paced traffic passed narrowly,
with p95 **4,947.024 ms**, p99 5,469.997 ms at 49.93 changed rows/s. Cleanup recorded
zero leaked or remaining descendants. The assertion fails before final table
comparison and subsequent continuous checks; this attempt is not a complete
continuous qualification pass.

The installed synthetic merge `25a9189` has parents SP01 main `485b552` and frozen
SP02 `838f0b1`. Paced-workload stage p95s include 2,702.155 ms before admission,
2,165 ms worker-start-to-prepared and 459 ms publication. These overlapping marginal
percentiles neither add to end-to-end p95 nor isolate the burst's cause.
[Issue #106](https://github.com/supabricks/platform/issues/106) retains this burst
failure separately from [#99's paced-workload misses](https://github.com/supabricks/platform/issues/99).
The same-head retry passed all installed sync suites: paced p95 **3,072.996 ms**,
p99 3,750.689 ms at 49.95 changed rows/s; burst **3,408.355 ms**, with clean teardown.
Both structured attempts are retained in the
[evidence archive](sync-performance-evidence/2026-09-26-sp02/ci/macOS-note.md).
Linux paired results cannot substitute for macOS qualification or attribute a
macOS regression; the five-second threshold remains unchanged.

## Provenance and reproduction

| Identity | Frozen value |
| --- | --- |
| Immediate predecessor | Merged SP01 `485b552ea5c802d4e446433b300f95851ce5aa6c`; runtime identical to accepted `b6a0b4b` |
| Candidate runtime and shared harness | `838f0b1d4977489526bbd6e77d036569f3df83a9` |
| Predecessor diagnostic manifest | `20a9d123799ed88c2b1e62d5079f33107223fb51984a0b149219b9a8afda9d91` |
| Candidate diagnostic manifest | `0e9337107a1a6e32c9c68bce28b36f45a7b0814861dfc5171465731b1e9278fe` |
| Shared diagnostic binary SHA-256 | `7413c196866cbb2527e3031582912c8933fdccb430307c273a29787dbf26e00a` |

The [evidence directory](sync-performance-evidence/2026-09-26-sp02/README.md)
contains complete inventories, source/bytecode overlay proofs, frozen analysis
modules, host records, cleanup receipts and original/exported checksums. Runtime
revision is an operator assertion backed by those inventories; the local diagnostic
overlays are not publisher-signed releases. The separate installed CI artifacts
retain their own build, package and synthetic-merge identities.

Run `component_analysis.py` against `component-screen/` and `profile_analysis.py`
against `main/` to reproduce the additional analysis. Each experiment's compressed
`analysis-source.json.gz` preserves the exact comparison modules needed to recreate
its summaries. No private fixture log, SQL or source row data is published. The
original [workflow profile](sync-workflow-profile.md) remains the historical
baseline; it is not substituted for the fresh SP01 arm.

## Contribution decision

**Keep — performance.** Bounded grouping repeatably reduces durable work per
captured transaction, preserves tested replay/feedback semantics, and turns all
six matched overload failures into complete, correct publications at the source
rate actually generated. The 10.92% paired median CPU increase at 16-core overload
is retained and investigated above, alongside the much larger volume of completed
capture/apply work. No new Linux low-load freshness or runtime failure appears.

The final evidence retains **48 full-stack trials** (24 mandatory plus 24 profiler
controls) and **33 component trials**, with zero contention replacements or cleanup
failures. Six predecessor drain timeouts remain failures. Earlier candidate and
macOS qualification failures remain separately archived; passing later attempts
do not erase them.

| Slice | Changed mechanism | Measured contribution | Remaining constraint | Decision |
| --- | --- | --- | --- | --- |
| SP02 | Bounded complete-transaction group commit with FULL/DELETE durability | Overload capture about 30 → 343–344 transactions/s; paired sync calls/transaction −93.38%/−93.48%; correct overload completion 0/6 → 6/6 | Actual source 684–689 changed rows/s, apply boundary scans and publication latency, macOS tails, sustained/power-loss qualification | Keep — performance; no 1,000-row/s claim |

The accepted next-slice predecessor is frozen runtime `838f0b1` with candidate
manifest `0e9337107a1a6e32c9c68bce28b36f45a7b0814861dfc5171465731b1e9278fe`.
SP03a first qualifies the actual bundled SQLite dependency before any WAL change;
SP03b must retain these group settings and separately measure journal mode.
Source qualification remains SP06. [Issues #87](https://github.com/supabricks/platform/issues/87),
[#91](https://github.com/supabricks/platform/issues/91),
[#96](https://github.com/supabricks/platform/issues/96),
[#99](https://github.com/supabricks/platform/issues/99),
[#106](https://github.com/supabricks/platform/issues/106) and
[#107](https://github.com/supabricks/platform/issues/107) retain the remaining
capacity, tail-latency and sustained-qualification work.
