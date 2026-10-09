# EQ228: growing-prefix worker memory

Initial component attribution; installed qualification pending.

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

Regression and installed growing-prefix results will be appended separately.

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
