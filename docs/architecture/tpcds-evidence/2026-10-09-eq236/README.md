# EQ236 bounded PostgreSQL cache qualification

Status: the opt-in cache profile and all four matched prefix trials passed their
configuration/correctness checks. Each trial loaded the same **14,770,127 rows**
through **14,434 byte-identical COPY transactions**. All 96 table checks passed:
generator versus PostgreSQL in the two isolated arms, PostgreSQL versus Delta
in the two concurrent-sync arms. This is one pair per mode, not a confidence
interval, full SF100 qualification, or an analytical SQL-suite result.

Selecting `source-load` gives each native compute **1 GiB of shared buffers**.
The default `compact` profile remains **128 MiB**. The setting persists through
restart and branch suspend/resume; conflicting later selections are rejected.
Use `supabricks up --compute-cache-profile source-load` with a new, short data
root. Each additional compute also gets the selected allocation; the option
does not raise the cell's memory limit. See [native configuration](../../native-cell.md).

The source-only whole-prefix throughput ratio is **1.656×**; the
concurrent load-plus-drain ratio is **1.100×**. These ratios compare
profiles within each mode. The isolated timer excludes source count checks;
the frozen concurrent timer includes them, so the two modes' whole-run times
must not be divided to estimate sync overhead.

## Same package, one configuration change

All arms use installed identity
`6d68820b47edbccee5f8095fe9bfedfd26d92577a88657253ef942694d24daa9`. The [package proof](package-proof.json) identifies
its exact source/build flags and predecessor; this is a diagnostic overlay,
not a signed release. Experimental lookahead stays off. Tests compare complete
rendered compute plans and confirm that only `shared_buffers` changes. Actual
PG settings in every observer receipt retain logical WAL, `fsync`, full-page
writes, synchronous commits, and disabled Neon local-file cache. Indexes, input
order and all COPY hashes are preserved.

The [predeclared plan](measurement-plan.json) keeps 8 logical CPUs (0–7),
16 GiB/no swap, COPY1024/4 MiB, backlog 65,536 rows, journal 16 MiB, decoded
values 32 MiB, plan 64 MiB, apply 768 MiB/300 s and worker recycling 512 MiB.
No observed OOM events occurred. Compiler observations and raw host disk/cgroup
samples are retained. Small status/evidence analysis continued on the host;
the 0.107-second postprocessor test run used CPU 8, outside the measured set.
The additional public-CLI smoke completed between timed trials.
No quiet admission gate or host cache drop was used. Physical storage is shared,
and one sequential pair cannot eliminate run-order or storage variation.

## Throughput and source phases

| Measurement | Source 128 MiB | Source 1 GiB | Sync 128 MiB | Sync 1 GiB |
| --- | ---: | ---: | ---: | ---: |
| Load + drain, seconds (source-only: load) | 543.4 | 328.2 | 897.2 | 816.0 |
| Whole-prefix rows/s | 27,180 | 45,009 | 16,463 | 18,101 |
| Late source-acknowledgment rows/s | 11,394 | 33,704 | 9,759 | 12,377 |
| Late mean COPY completion, ms | 66.932 | 4.524 | 69.259 | 5.136 |
| Late mean COMMIT, ms | 18.247 | 21.051 | 20.182 | 25.310 |
| Late index block reads (narrower counter window) | 763,639 | 0 | 770,821 | 0 |
| Late writer getpage wait, seconds (narrower window) | 94.541 | 0.000 | 97.903 | 0.000 |
| Late average cell CPU, logical cores | 1.441 | 1.411 | 2.488 | 1.946 |
| Late memory full-pressure stall, seconds | 0.033 | 0.058 | 0.050 | 0.043 |
| Late I/O full-pressure stall, seconds | 3.514 | 3.305 | 3.148 | 6.508 |
| Peak observed cell memory, GiB | 16.001 | 16.001 | 16.001 | 16.001 |

The late source cohort is exactly the same 1,562 whole transactions / 1,599,488
rows in every arm, selected by acknowledgments at 13.0–14.6m rows. First and
last acknowledged totals are 13,000,655 and 14,599,119. COPY and COMMIT client
spans are disjoint. Server/storage counters overlap them and must not be added.
Storage and cgroup observations use narrower two-second windows; their exact
endpoints remain in `comparison.json`, not silently aligned to the transaction
cohort. Zero index/getpage deltas mean zero new events in that sampled window,
not zero page activity over the complete trial.

The isolated comparison removes the measured index-fetch bottleneck at this
prefix: COPY completion drops sharply and durable COMMIT becomes the largest
remaining source phase. This is cache placement within the same cell budget,
not evidence that more CPU cores or a larger cell limit are needed. Any remaining
sync slowdown must be assessed from the concurrent measurements separately.

## Publication throughput and lag

The following late publication windows round up to actual publication boundaries
at 13.0m and 14.6m rows. They differ from the exact source cohort above.

| Concurrent profile | Publication window | Rows/s | Post-commit event lag p95 | Maximum sampled backlog |
| --- | --- | ---: | ---: | ---: |
| 128 MiB | 13,004,751–14,601,167 | 9,845 | 4.706 s | 65,536 |
| 1 GiB | 13,016,015–14,613,455 | 12,315 | 5.201 s | 65,536 |

Lag joins each instrumented client acknowledgment to the first covering
publication by exact cumulative rows in this single-loader, insert-only fixture.
The `apply.published` event occurs after the SQLite publication/head commit;
one millisecond is added for timestamp truncation. The two processes use the
same host wall clock. This bound includes event emission delay. All required
events join once, and publication instrumentation reports no errors/drops.

A second, looser bound uses the next sample's start (or the final completed
checkpoint) after the first covering publication read. Both distributions are
retained. [#243](https://github.com/supabricks/platform/issues/243) explains why
older pre-read sample timestamps were estimates rather than strict upper bounds;
do not compare their labels directly with these corrected bounds. Raw historical
receipts are unchanged.

The p95 event-bound lag **increases by 0.495 seconds** in the candidate;
throughput and latency do not improve together in this pair. Whole-prefix
throughput improves 9.95%, and late publication throughput improves 25.1%.
The candidate's 18,101 rows/s remains below the 20k target.

## Remaining pipeline costs

Whole-run explicit backlog pacing increases from 228.1 to 392.1 seconds. Within
the matched late source cohort, all non-transaction gaps grow from 23.6 to
81.2 seconds; those gaps include harness work as well as pacing and must not
be treated entirely as sync waits. The faster source increasingly waits for
publication capacity. Late average cell CPU falls from 2.49 to 1.95 cores,
principally alongside lower pageserver activity; this is not evidence of
cell-wide CPU saturation. Cgroup peak memory includes filesystem cache and
brief kernel-accounting overshoot; it is not worker RSS. No sampled OOM occurs.

The retained per-batch join gives the following medians in the publication
windows above. They describe different resulting batch sizes; they are not
fixed-size microbenchmarks. Planning contains decode and key scanning, and
worker duration contains planning, apply and durability: **do not add them**.

| Late batch metric | 128 MiB | 1 GiB |
| --- | ---: | ---: |
| Batches | 70 | 49 |
| Rows per batch | 22,016 | 31,744 |
| Worker duration | 1,758.5 ms | 1,968.3 ms |
| Complete planning | 930.6 ms | 1,110.2 ms |
| Existing-key scan | 533.0 ms | 541.2 ms |
| Decode | 214.3 ms | 331.9 ms |
| Table apply | 335.3 ms | 413.2 ms |
| Durability | 198.5 ms | 140.8 ms |
| Done to publication | 264.0 ms | 237.6 ms |

Both arms consume their complete selected journal ranges and reach every
selected target; larger journal limits would not repair a truncation here.
The candidate still spends roughly half its median worker time in planning.
Its durability p95 is 1,059.9 ms versus 560.5 ms, and late I/O pressure is higher.
These traces warrant a separate measured planning/key-scan and durability slice
tracked in [#263](https://github.com/supabricks/platform/issues/263);
they do not establish disk bandwidth saturation or justify raising hard limits.
`c128-batch.json` and `c1024-batch.json` retain all joined batches and health
checks. `replay_batch.py` reproduces them from compressed events, COPY ledgers,
and a minimal publication-table projection, without retaining private state.

## Correctness, failures and limits

`installed-lifecycle.json` checks both allocations against actual PostgreSQL,
durability settings, second-branch creation, suspend/resume, restart without
flags and conflicting selections. `installed-up.json` separately checks a fresh
public `up` and no-flag reconnect. Rust validation includes 235 passing local
unit tests, the passing cache persistence/legacy-default test, and two core
rendering/golden tests. All 49 TPC-DS harness tests pass.

Retained failed attempts:

- `s128` failed before COPY with a source receipt's wrong branch-ID field.
  [#242](https://github.com/supabricks/platform/issues/242) is fixed by `6cc423f`;
  the fresh `s128b` control has its own receipts and no source batch was replayed.
- The initial untimed lifecycle fixture exceeded the existing Unix-socket path
  limit. Its error/log remain under `failed/`; shorter fixture paths passed.
- The initial Rust persistence fixture lacked required dummy engine files.
  The 235 unit tests passed in that run; completing the fixture in `9f1fc2f`
  made the separate persistence check pass. Its initial log is retained.

The 1-GiB cache accommodates the active index working set in this measured prefix. **Growth beyond 14.77m rows
has not been qualified**: larger/random-key indexes and later fact tables can
exceed it. These results do not establish the 20k/10× target across SF100,
full-dataset fit, or production latency guarantees. Full bootstrap of an already
large source is still tracked separately in [#237](https://github.com/supabricks/platform/issues/237).
The original SF100 container remains paused and SP remains frozen.

## Reproduce the evidence review

From the repository root:

```sh
python3 e2e/tpcds/compare_compute_cache.py \
  docs/architecture/tpcds-evidence/2026-10-09-eq236 \
  --labels s128b s1024 c128 c1024 --output /tmp/eq236-replay.json
cmp /tmp/eq236-replay.json docs/architecture/tpcds-evidence/2026-10-09-eq236/comparison.json
python3 docs/architecture/tpcds-evidence/2026-10-09-eq236/replay_batch.py
(cd docs/architecture/tpcds-evidence/2026-10-09-eq236 && sha256sum -c SHA256SUMS)
```

The comparison rejects different package/settings/COPY inputs, missing exact
checks, observation errors/drops, changed resource limits and observed OOMs.
It replays compressed raw ledgers and retained publication boundaries without
restarting a server. `provenance.json` binds the analysis and verifier sources;
individual launch receipts bind the frozen loader and diagnostic copies.
