# EQ230: batch fill and publication attribution

Diagnostic qualification complete: 14,770,127 rows published and exact typed
PostgreSQL-versus-Delta comparison passed all 24 tables. This is the growing
SF100 prefix, not full SF100 ingestion or analytical-query qualification.
No batching, authority, durability or resource policy changed.

## Findings

The predeclared 13.0–14.6m publication window contains 73 batches. Every batch
reached its authorized target and decoded the complete selected journal range.
Median batch size is 21,504 rows; maximum is 46,080. Median input is 4,490,061
bytes; maximum is 9,633,184 bytes. Neither the 65,536-row nor 16-MiB journal
limit caused these small batches.

At admission, a median 23,552 changed rows were durable but only 21,504 were in
the selected target. Median report age is 217 ms, excluding a median 2,048
already-durable rows. By the owner read, a median 3,072 durable rows remain
beyond the target. Median undurable committed backlog at admission is zero
(p95 1,024); capture is usually close to source progress. Medians describe
individual distributions and must not be subtracted to derive other medians.

Source COPY plus COMMIT averages 98.334 ms per 1,024-row transaction in the
same row cohort (p50 88.722 ms; p95 145.510 ms). Its 1,560 transactions consume
153.402 seconds of active source-call time; the publication interval takes
178.231 seconds. That is about 10,400 rows/s during active source calls under
simultaneous sync, **not an isolated PostgreSQL capacity limit**. These are
row-matched cohorts, not identical clock intervals. Whole-run source calls
consume 591.907 s, flow-control waits 223.854 s, and total loading 915.010 s.
Median source commit to durable capture is 45.711 ms in the late cohort
(p95 197.156 ms). Source commit timestamps come from validated pgoutput commits;
COPY+commit latency comes from the unchanged loader.

The apply side is also material. Late medians, measured inside the worker:

| Measurement | Median |
| --- | ---: |
| Journal request | 19 ms |
| Decode | 207 ms |
| Existing-key scan | 530 ms |
| Complete plan, including decode and key scan | 924 ms |
| Delta table apply | 336 ms |
| Inventory | 90 ms |
| Durability calls | 177 ms |
| Complete worker call | 1,795 ms |
| Worker return to done marker observation | 33 ms |
| Done marker observation to publication commit | 248 ms |
| Prior publication to next admission | 64 ms |
| Admission to dispatch | 30 ms |
| Dispatch to worker entry | 65 ms |

Spans are nested; adding these medians would double count work. In particular,
plan contains decode and key scan, and worker time contains durability. The
older receipt-based worker/after-manifest split uses different boundaries.
Durability p95 is 1,073 ms; retain these outliers. Warm previous-version
verification is 61.7 ms median (70 requests), versus 880.5 ms for the three
first requests in new workers. Four process identities serve the late window.

Sampled late cell CPU averages 2.412 cores: pageserver 0.831, apply 0.706,
PostgreSQL 0.188, daemon 0.170, SeaweedFS 0.166 and capture 0.123. The sampled
window writes 15.375 GB on device 259:0. Cgroup memory reaches its 16-GiB bound;
this includes file cache. Process counters miss short-lived tails, and these
observations alone do not establish CPU, disk saturation or memory pressure as
the cause of source-call latency. See the precise sampled endpoints in
`profile-resources.json`; they differ from exact publication endpoints.

**Next slice:** [#232](https://github.com/supabricks/platform/issues/232) separates
COPY streaming/completion, the LSN query and COMMIT, then attributes PostgreSQL
index/page reads, Neon pageserver requests and WAL/safekeeper/storage waits.
Compare source-only and simultaneous-sync controls at the same data size.
Source supply and apply costs are close enough that neither should be declared
the sole bottleneck. Target refresh can recover a few thousand rows; the
measurement does not justify increasing limits or adding a fill delay. Key-scan
cost remains a separate potential slice requiring bounded metadata and exact
lookup/lease/corruption coverage; do not restore #228's unbounded footer cache.

## Controlled comparison and limitations

`measurement-plan.json` predates the run. Both packages use the same frozen
harness `0bc57c170cb6e34f7d3cefa1ae6981c9b1e8d090`, CPUs 0–7, 16 GiB/no swap,
COPY1024/4 MiB and backlog65536. Journal16 MiB, values32 MiB, plan64 MiB,
apply768 MiB/300 s and recycle512 MiB remain unchanged. Cross-batch preparation
is disabled. Original `eq03-sf100-load-01` stays paused; SP remains frozen.

| Measurement | Unprofiled #228 `c228a` | Profiled `c230a` |
| --- | ---: | ---: |
| Whole prefix | 16,692.8 rows/s | 16,129.4 rows/s |
| Fixed late publication window | 9,462.2 rows/s | 8,962.8 rows/s |
| Sampled p95 commit-to-publication upper bound | 5.026 s | 5.424 s |
| Late publications | 72 | 73 |
| Median late rows per publication | 22,016 | 21,504 |

The profiled run is 3.4% slower overall and 5.3% slower late. This comparison
combines observer overhead and run variability; it does not isolate either or
claim a performance improvement. No compiler samples or concurrent local
build/test/profiling workload occurred during the timed trial. Exact checks
started only after the container finished. The 20k/10× target is still unqualified.

## Instrumentation and integrity

Clean source `1af6e10d3944f8ecd47f5f474d0c8350133b0017` built the diagnostic
package, identity `b42e32d688bab10d577f485a6a7e2316e85f8ef71e94e4f9d53301187dcae08c`.
`package-proof.json` binds the base identity, binary, overlay and worker hashes
and successful installation verification. The package builder rejects dirty
source, stale workers and stacked profiling hooks. Python overlays exist only
in this diagnostic package. Rust events require the existing `sync-profile`
feature and private enable marker; disabled builds do not evaluate event fields.
The wrapper enables profiling around the frozen loader without editing it.

Events contain scalar counters, request IDs, LSNs and timestamps, never source
field values or SQL. Transaction frame counts are observed after durable append;
no extra journal query is issued. Owner selection events are written after its
snapshot has closed. Traces have 8-MiB per-process budgets and no fsync; I/O
failures are counted and do not fail the operation. The daemon has separate
bounded event and cumulative-span streams. Counts/timestamps are diagnostic,
never authority for capture ACK, target selection or publication.

All 14,434 positive-row capture transactions match the source ledger by exact
row count, order and bounding LSN intervals; WAL byte distances do not estimate
rows. All 515 incremental publications join their admission, dispatch, worker
and publication events. One early worker was retired after writing its actual
done marker but before logging the diagnostic event: retain its worker-end to
publication bound and omit its unavailable done split. All 73 late batches have
complete handoff events. Every observed process/daemon health record has zero
errors and drops; every file remains well below its budget. Process writes
accumulate about 2.20 s, capture observation about 4.28 s (including its writes),
and daemon writes 0.146 s. These counters overlap and exclude some observer
costs, so they are not an additive overhead estimate.

`profile-selection.json` freezes events before the timed container's finish.
The later exact verification restarts the native cell; its diagnostic events
are excluded. Rust event clocks have millisecond resolution. All failed and
outlier benchmark observations remain in the retained trace.

Validation: 233 Rust library tests passed (4 ignored), the enabled profile
budget test passed, and a separate disabled-build test proved closures are not
evaluated. Five diagnostic/join tests passed. A real one-row installed apply
verified correlation, its ready receipt and absence of source values. All 19
owner protocol tests passed with hooks enabled. Their intentionally invalid
non-pgoutput payloads produce 27 diagnostic parse errors, explicitly retained in
`owner-smoke.log`; the real installed trial has zero. These unit tests are not
additional throughput trials.

## Reproduce the attribution

From the repository root, with a fresh output path:

```sh
python3 e2e/tpcds/analyze_batch_profile.py \
  --load docs/architecture/tpcds-evidence/2026-10-09-eq230/c230a \
  --profile docs/architecture/tpcds-evidence/2026-10-09-eq230/profile \
  --publications docs/architecture/tpcds-evidence/2026-10-09-eq230/publications.jsonl \
  --output /tmp/eq230-replayed.json
```

`publications.jsonl` is a minimal read-only export of exact publication IDs,
timestamps and row totals, sufficient to reproduce the same attribution without
the private state database. The archived replay and live-database analysis are
byte-identical. Raw commits, observations, resource samples, launch/cleanup
receipts, test logs and helpers are included. Large data files are not included.
`SHA256SUMS` covers every evidence file except itself. Raw cells remain at
`/data2/supabricks-eq/eq220/{c230a,vc230a}`.

PRs #222 and #229 remain unmerged because CI is red: #231 (repeated macOS suite
stall), #233 (Linux reuse fixtures), #215 (stale console assertion) and #227
(offline export). No failing gate is bypassed. This does not invalidate the
local measured prefix, but it blocks merging and any release qualification.
