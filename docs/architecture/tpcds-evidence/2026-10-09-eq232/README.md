# EQ232 source-write attribution

Status: both timed arms completed, with exact checks passing all 24 tables in
each arm: generated prefix versus PostgreSQL for the isolated control, and
PostgreSQL versus Delta for concurrent sync. No production configuration or
performance improvement is claimed by this diagnostic slice.

The dominant late source cost is **primary-key page reads during COPY**, present
with and without analytics sync. The installed PostgreSQL has 128 MiB of shared
buffers and no Neon local file cache. Its loader waits for roughly 766–771k
synchronous page fetches in the sampled late window. Concurrent sync adds a
second cost: COMMIT latency and more storage writes/stalls. First qualify a
bounded PostgreSQL cache profile in [#236](https://github.com/supabricks/platform/issues/236),
keeping the 16-GiB cell limit. Full-source bootstrap has a separate size limit,
tracked in [#237](https://github.com/supabricks/platform/issues/237).

## Matched transaction cohort

Both arms commit the same 14,770,127 rows through **14,434 byte-identical COPY
transactions**, including table order, offsets, encoded sizes and payload
SHA-256 values. Every phase event joins its attempt and acknowledgment. There
are zero diagnostic errors/dropped events and no observed compiler activity.

The predeclared late cohort contains 1,562 complete transactions / 1,599,488
rows, selected by source acknowledgments at 13.0–14.6m rows. The first and last
acknowledgment totals are 13,000,655 and 14,599,119. This is different from
EQ230's **publication** cohort; their late rates are not interchangeable.

| Late source measurement | Isolated PostgreSQL | Concurrent sync |
| --- | ---: | ---: |
| Cohort wall time | 139.809 s | 175.327 s |
| Rows/s including inter-transaction gaps | 11,441 | 9,123 |
| Rows/s during active transaction spans | 12,018 | 10,499 |
| Mean BEGIN | 0.061 ms | 0.080 ms |
| Mean COPY entry | 0.074 ms | 0.096 ms |
| Mean COPY streaming | 0.027 ms | 0.029 ms |
| Mean COPY completion | **69.147 ms** | **70.898 ms** |
| Mean pre-commit LSN execution | 0.064 ms | 0.072 ms |
| Mean COMMIT | **15.745 ms** | **26.270 ms** |
| Original harness COPY+commit mean | 85.333 ms | 97.815 ms |
| Inter-transaction gaps, aggregate | 6.717 s | 22.974 s |

COPY completion is 108.008 versus 110.743 aggregate seconds; COMMIT is 24.593
versus 41.033 seconds. Phase timers are disjoint client spans. Server/storage
counters below overlap those spans and must not be added to them. The small
residual inside each transaction is reported in the machine-readable analysis.
Inter-transaction gaps include encoding, ledger work, scheduling and harness
status/storage sampling; they are **not entirely attributable to sync pacing**.

The complete concurrent run publishes all rows in 930.502 s load + 0.178 s drain
(~15,870 rows/s). Its total backlog pacing is 221.013 s, concentrated earlier in
the run; a separate exact late pacing duration was not recorded. The isolated
load takes 537.653 s and has no publication pacing. Its reported load timer ends
before source count checks; the concurrent frozen timer includes those checks.
Use the matched transaction cohort for the source comparison, not a ratio of
these differently scoped full-run times.

## PostgreSQL, Neon and host observations

Two-second counter samples lie strictly inside the transaction cohort. Their
endpoints differ slightly: source-only 13,011,919→14,580,687 rows over 137.559 s;
concurrent 13,017,039→14,588,879 over 172.341 s.

| Counter in its stated sample window | Isolated PostgreSQL | Concurrent sync |
| --- | ---: | ---: |
| `store_returns` primary-key index block reads | 765,888 | 766,266 |
| Loader Neon getpage wait count | 765,901 | 770,772 |
| Loader Neon getpage wait time | **98.068 s** | **98.875 s** |
| Safekeeper WAL flush count | 2,101 | 2,507 |
| Safekeeper WAL flush time | 20.221 s | 34.290 s |

The index counts are database-wide relation statistics; Neon counters are
scoped to the loader PID. Their close agreement, the loader's `Neon/PS_ReadIO`
wait samples, and the COPY completion spans identify repeated index-page fetches
as the dominant late source cost. The cache change is a measured next candidate,
not a proven speedup. Safekeeper timing and increased COMMIT/`SyncRep`/`WalSync`
waits identify a second durability/storage cost; they do not prove device
bandwidth saturation or justify relaxing durability.

Cgroup samples have their own narrower windows (137.925 / 172.496 seconds):

| Resource observation | Isolated PostgreSQL | Concurrent sync |
| --- | ---: | ---: |
| Total cell CPU, average logical cores | 1.468 | 2.519 |
| Sampled pageserver CPU, cores | 0.982 | 0.882 |
| Sampled PostgreSQL CPU, cores | 0.193 | 0.204 |
| Sampled apply CPU, cores | none | 0.744 |
| Device 259:0 cgroup writes, decimal GB | 9.285 | 16.466 |
| Direct reclaim pages scanned | 828,828 | 1,924,249 |
| Memory pressure `full` stall | 0.026 s | 0.065 s |
| I/O pressure `full` stall | 2.265 s | 6.464 s |
| Final anonymous / file memory, GiB | 2.470 / 13.003 | 3.158 / 12.373 |
| OOM / OOM kills | 0 / 0 | 0 / 0 |

Both cells approach the 16-GiB limit, largely through file cache. Reclaim is
visible, but measured memory-pressure stall is small. Increasing the global hard
limit is not the first experiment indicated by these data. An explicit PG cache
profile can test the smaller internal cache while retaining the cell budget.
Process samples can miss retired process tails; cgroup CPU is authoritative for
the cell total. Host disk counters are shared and cannot establish isolated
hardware saturation.

## Correctness and the bootstrap finding

`vs232a` compares all 24 PostgreSQL tables against independent typed generated
rows, preserving NULLs, fixed CHAR padding, exact decimals, dates and duplicate
multiplicity. It also re-encodes every loaded COPY payload with the frozen
batcher and checks the original acknowledgment ledger. `vc232a` performs the
existing exact typed PostgreSQL-versus-Delta comparison for all 24 tables.

The original source-only harness attempted a post-load snapshot for that second
kind of verification. Its 180-second readiness helper expired after the timed
load completed; the source and timing ledgers remained intact and cleanup left
no processes. `s232a/result.json` **retains that FAIL**. Direct source verification
has a separate `SOURCE_EXACT_PASS` receipt tied to its SHA-256. It retires only
that failed post-timing enrollment and does not replay COPY or overwrite the
original receipt.

Inspection then found that full export permits only 1,024 batches of at most
4,096 rows across the entire generation: at most 4,194,304 rows, fewer with
partial/empty tables or byte-bound batches. Thus this 14.77m preloaded control
cannot bootstrap even with a longer helper timeout. The observed failure was
a harness timeout, **not an observed metadata-budget rejection**. The bound is
independently established from the installed source. The policy also has a
1-GiB / 300-second export limit. [#237](https://github.com/supabricks/platform/issues/237)
tracks a bounded large-source bootstrap; #232 raises none of these limits.
The final source-only harness does not attempt that unsupported bootstrap.

## Reproduction and limits

The [predeclared plan](measurement-plan.json) retains the initial intended
snapshot verification. The resolved source-oracle substitution above is an
explicit qualification change, not a discarded failed trial. The concurrent
arm runs frozen loader revision `0bc57c170cb6e34f7d3cefa1ae6981c9b1e8d090`.
Both use package `v0.1.0-alpha.36.eq230profile`, identity
`b42e32d688bab10d577f485a6a7e2316e85f8ef71e94e4f9d53301187dcae08c`.

Both keep CPUs 0–7, 16 GiB/no swap, COPY1024/4 MiB, synchronous commits, fsync,
full-page writes, logical WAL and all indexes/identity. Concurrent sync retains
backlog65536, journal16 MiB, values32 MiB, plan64 MiB, apply768 MiB/300s and
recycle512 MiB. PG writer IO clocks are enabled in both diagnostic arms. Waits
are sampled every 20 ms, storage counters every two seconds. Sampler accumulated
wall time is 13.239 / 28.447 seconds; it is not subtracted from throughput.
The two bounded ledgers stay below 64 MiB each.

This is one diagnostic pair. The concurrent whole-run rate is ~1.6% below EQ230;
that combines added observation/IO-clock costs and run variance, not an isolated
overhead estimate. No host cache was flushed; full input validation reads the
generated dataset in both arms. Full SF100, the SQL suite, 20k rows/s and a 10×
improvement remain unqualified. Original SF100 load-01 stays paused; SP frozen.

For new trials, use fresh labels with `e2e/tpcds/run_source_profile.py` and then
`verify_source_only.py` / `verify.py`. To replay the archived arithmetic, the
analyzer reads deterministic `.jsonl.gz` receipts directly:

```sh
EVIDENCE=docs/architecture/tpcds-evidence/2026-10-09-eq232
python3 e2e/tpcds/analyze_source_profile.py "$EVIDENCE/s232a" \
  --qualification "$EVIDENCE/vs232a/result.json" --compare "$EVIDENCE/c232a" \
  --output /tmp/eq232-source.json
python3 e2e/tpcds/analyze_source_profile.py "$EVIDENCE/c232a" \
  --compare "$EVIDENCE/s232a" --output /tmp/eq232-concurrent.json
```

The archive contains input/phase joins, wait/storage/cgroup observations,
source-bound wrapper copies, exact-table receipts, cleanup outcomes and hashes.
The archived source control installs diagnostic views on its first connection;
the concurrent wrapper prepares them before capture enrollment to avoid a DDL
fence. Timing and sampling behavior are otherwise identical. Bootstrap limits
and interrupted receipt details are retained in `bootstrap-bound.json`.
