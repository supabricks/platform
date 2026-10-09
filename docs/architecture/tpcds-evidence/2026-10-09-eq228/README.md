# EQ228: growing-prefix worker memory

Component attribution and matched installed growing-prefix qualification complete.
Both cells pass exact typed PostgreSQL-versus-Delta verification across all 24 tables.

The private probe reconstructs the exact published `lgrow` 14,770,127-row
inventory, then applies the next 32,768 actual SF100 `store_returns` rows.
It generates complete 1,024-row pgoutput transactions with synthetic LSNs in a
separate private spool. This excludes source transport, ACK, controller
publication, admission and concurrent source load; it is not throughput evidence.
The original SF100 load remains paused and all source evidence is read-only.

On eight logical CPUs, the baseline `cold02` scan returned zero existing keys
but grew RSS from 258.6 to 464.5 MiB. The complete apply peaked at 556.0 MiB,
above the unchanged 512-MiB reuse threshold; RSS after collection was 494.0 MiB.
The `nocache01` candidate uses Arrow's `cache_metadata=False` for that single
scan. Its complete apply peaked at 411.8 MiB and finished at 336.8 MiB after
collection. No worker, row, value, input, deadline or durability bound changed.

The cold baseline hashed 1,354,643,144 bytes in 0.695 seconds. A separately
primed warm baseline (`warm01`) hashed only 3,040,365 new bytes, with 0.072 seconds
spent in all digest calls. Timings include diagnostic wrappers, use a warm OS
page cache and have not yet been repeated. `cold01` was exploratory without
CPU affinity; retain it but exclude it from matched comparisons. Nested stage
times must not be added. Memory comes from Linux process status, not Arrow's
allocator alone; native Parquet metadata is not all counted by Arrow's pool.

Regression and installed growing-prefix results follow below.

## Repeated real-mailbox result

All 12 runs (three alternating pairs, each cold/warm) seal exactly the same plan.
All six baseline `done.json` markers say `reusable=false`; all six candidate
markers say `reusable=true`, using the actual unmodified mailbox lifecycle and
512-MiB high-water rule. The probe records `getrusage` as used by that rule.
All 211 Python tests pass, including corruption, leases, replay, exact composite
lookup, missing statistics and historical versions. See `component-summary.json`.

Retain the candidate cold pair 3 outlier: 9.144 s total, including 5.053 s in the
final durability call and 0.978 s flushing the table. The scan itself took 0.641 s.
Do not omit fsync time or use these diagnostic runs as an end-to-end benchmark.

The archived `prepare.py`, `probe.py` and `repeat.py` reconstruct this experiment;
substitute `<repo>` / `<evidence-root>` with the local paths, run from the repo,
and use the installed package's pinned Python for preparation. The script uses
fresh clone directories and never writes the source generation or source spool.
`fixture02` followed a retained setup failure from using the host's unqualified
SQLite (`fixture`, before any fixture transactions). Pair preparation/copying,
cache priming and six-second mailbox idle exit are outside the apply timer.
Source override in the candidate is exactly the one-line scanner option plus
comment in this commit; installed source-bound package qualification follows.

## Installed matched pair

`comparison-plan.json` freezes the profiles and comparison window before the
candidate's timed load. The current serial baseline is `eq226serial` / `c7af44c`;
candidate `eq228a` / `e4914bf` changes only `incremental_worker.py` and its compiled
Python cache. `package-proof.json` binds the complete clean source and installed
package. Cross-batch preparation is disabled in both packages.

| Measurement | Baseline `c228b` | Candidate `c228a` |
| --- | ---: | ---: |
| Whole 14,770,127-row prefix | 15,761.7 rows/s | 16,692.8 rows/s |
| Predeclared 13.0–14.6m publication window | 8,437.4 rows/s | 9,462.2 rows/s |
| Sampled late apply process identities | 44 | 4 |
| Maximum sampled late apply RSS | 562.4 MiB | 441.6 MiB |
| Sampled p95 commit-to-publication upper bound | 6.344 s | 5.026 s |
| Exact late-window publications | 52 | 72 |
| Median late rows / publication | 31,744 | 22,016 |
| Median late worker time | 3,144 ms | 1,596.5 ms |
| Median late after-manifest time | 448.5 ms | 409 ms |

The gains are 5.9% overall and 12.1% in the late window in this single pair.
Timing excludes input admission, bootstrap and exact checks; includes load plus
drain. Two-second resource samples cannot establish hard RSS peaks or complete
CPU attribution for short-lived processes. Their endpoints differ slightly from
exact publication boundaries. The full prefix's lag is an observation-based
upper bound. Nested phase timings are not additive.

Both timed cells used CPUs 0–7, 16 GiB without swap, COPY1024/4 MiB,
65,536 unpublished rows, and unchanged journal/value/plan/deadline/worker bounds.
Both observed a maximum backlog of exactly 65,536. Neither observed a compiler;
no local build, test, profile or exact check overlapped either timed run. These
results qualify this prefix only, not full SF100 ingestion or TPC-DS queries.
The original SF100 container remains paused and SP remains frozen.

`installed-comparison.json`, per-mode summaries, batch costs, sampled resources,
raw load results/commits/observations and cleanup/limit receipts retain the
comparison. `compare.py` checks that batch-row totals match the exact late
publication window. The runner uses frozen harness revision
`0bc57c170cb6e34f7d3cefa1ae6981c9b1e8d090`; package and harness identities are in
its launch receipt. `verify-pair.py` refuses to start until both timed cells
have finished successfully, then runs exact typed checks sequentially under the
same CPU/RAM limits.

Worker churn improves substantially. Smaller batches and more publications
limit the resulting throughput gain; their cause is not yet established.
[#230](https://github.com/supabricks/platform/issues/230) tracks capture supply,
target freshness, batch fill and publication-cost attribution before the next
implementation change. The 20k/10× target is still unqualified.

Text copies replace repository and evidence-root paths with `<repo>` and
`<evidence-root>`. SHA256SUMS covers these archived bytes; hashes inside receipts
bind original source, package or raw evidence and can differ after sanitization.
Private spool/database/runtime credentials are not exported.
