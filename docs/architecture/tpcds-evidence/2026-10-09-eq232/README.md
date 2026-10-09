# EQ232 source-write attribution

Status: instrumentation validated; source timing completed, concurrent timing
and exact qualification in progress. The source control's post-load snapshot
exceeded the old three-minute empty-bootstrap helper deadline. Its original
failed receipt and complete timing ledger are retained. A separate post-timing
qualification resumes the stopped cell with a 15-minute readiness bound; it
does not replay COPY or overwrite that original receipt.
[Issue #232](https://github.com/supabricks/platform/issues/232) follows the
completed EQ230 batch/publication attribution. This is a diagnostic slice;
there is no production configuration change or performance improvement claim.

The predeclared [measurement plan](measurement-plan.json) uses the same
14,770,127-row growing prefix, table order, input bytes, COPY1024/4 MiB,
eight CPUs and 16 GiB without swap. Concurrent sync retains its 65,536-row
publication window and all worker limits. The original SF100 cell stays paused.

The concurrent arm executes the frozen `load.py` through a connection proxy.
The source-only control reuses its locked workload, validation and batching,
and issues identical COPY/LSN/COMMIT statements. It has no capture/apply policy
while timed, preserves logical WAL, replica identity/indexes and synchronous
durability, and bootstraps sync after timing for the same exact verifier.
Both complete COPY attempt ledgers must match, including SHA-256 of every
encoded payload. Source-only throughput excludes this post-load bootstrap.

`source_profile.py` separates BEGIN, COPY entry, streaming, completion,
pre-commit LSN execution and COMMIT. Failures retain the original rollback or
ambiguous commit and are never retried. Numeric phase records cannot expose
SQL, credentials or source values. Each ledger is bounded at 64 MiB; drops,
write errors or missing joins invalidate attribution. PostgreSQL writer IO
clocks are enabled in both diagnostic arms. A separate connection samples
writer wait state every 20 ms and PG/Neon/storage counters every two seconds.
Counters and sampled waits overlap; their totals must not be added as disjoint
phases. The sampler's own accumulated wall time is reported.

The late cohort is defined by **source acknowledgments at 13.0–14.6 million
rows**, unlike EQ230's publication cohort. Counter comparisons use the first
and last two-second samples strictly within that cohort and report those
narrower endpoints. Host disk counters are shared; cgroup counters isolate
this cell. Neither alone establishes device saturation. No host cache is
flushed. Input validation reads the dataset in both arms.

Run from the repository root with the existing frozen input/package fixtures:

```sh
python3 e2e/tpcds/run_source_profile.py s232a \
  build/eq220/programs/releases/v0.1.0-alpha.36.eq230profile source_only
python3 e2e/tpcds/run_source_profile.py c232a \
  build/eq220/programs/releases/v0.1.0-alpha.36.eq230profile concurrent
python3 e2e/tpcds/analyze_source_profile.py /data2/supabricks-eq/eq232/s232a \
  --compare /data2/supabricks-eq/eq232/c232a --output build/eq232/source-only.json
python3 e2e/tpcds/analyze_source_profile.py /data2/supabricks-eq/eq232/c232a \
  --compare /data2/supabricks-eq/eq232/s232a --output build/eq232/concurrent.json
```

The launcher preserves a frozen harness archive, exact command, release/source
identity, container limits, cleanup receipt and two-second process/cgroup
samples. Use fresh labels; existing results cannot be overwritten. Independent
24-table verification runs only after both timed arms have stopped.
