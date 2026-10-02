# SP09b — Table parallelism prerequisite assessment

Status: **assessment complete; concurrency implementation deferred**, 2026-10-02
UTC. The post-SP09a two-table reference workload does not establish CPU-bound
apply work. Keep table concurrency at one. No concurrency speedup is claimed and
no concurrency-2/4 candidate has been implemented or qualified.

## Evidence and decision

The [implementation plan](../plans/sync-performance-implementation.md#sp09b--bounded-table-parallelism-only-if-apply-becomes-cpu-bound)
requires profiling to justify parallel preparation/merge before adding it.
The [reproducible screen](sync-performance-evidence/2026-10-02-sp09b-screen/screen.json)
reads all 18 accepted SP09a candidate main trials, verifies their archived artifact
hashes, and uses final successful warm-request profiles. It covers 1,908 warm
requests, including 1,319 at qualified load. Cold, failed/incomplete and
outside-window requests are explicitly counted; none of the selected measurement
windows contains a failed/incomplete request. The source workload updates two
tables in each transaction, and median apply-table calls per request are two.

At 1,250 offered rows/s and approximately 1,249 achieved rows/s:

| Logical CPUs | Warm apply wall time | Request CPU time | CPU seconds / apply wall second | Python fsync wall time | Delta merge wall time, both tables |
| --- | --- | --- | --- | --- | --- |
| 8 | 355–360 ms | 231–233 ms | 0.647–0.650 | 168–171 ms | 16.7–16.9 ms |
| 16 | 357–360 ms | 224–228 ms | 0.627–0.633 | 173–174 ms | 17.4–17.5 ms |

Values are ranges of the three trial medians, not pooled request percentiles.
Per-request Python `fsync` spans account for a median 47.4–49.3% of apply wall
elapsed time. Request CPU includes all worker threads; it is a request-local delta,
not repeatedly summed process lifetime counters. These observations indicate
substantial durability waiting and do not show a saturated compute path to
parallelize. Whole-stack CPU is approximately one core despite 8/16 logical CPUs
being available. Low whole-stack utilization alone would not establish this;
request-level CPU and wall-time spans provide the additional evidence.

The existing serial table loop includes durable writes and boundary checks, not
just CPU work. Parallelizing it could overlap some waits, but could also increase
storage contention and requires aggregate reservations, cancellation and safe
partial-commit recovery. This screen does not prove that overlapping I/O cannot
help; it establishes that the plan's CPU-bound prerequisite is not met. Delta
merge currently takes only about 17–18 ms across both tables. Adding a pool now
would introduce those obligations without measured evidence for a CPU bottleneck.

## Measurement limits

Inclusive table, merge and fsync spans overlap; do not sum them or subtract
independent percentiles to derive a speedup. CPU and wall measurement boundaries
differ by small profiler wrapper overhead. Python fsync timing does not capture
all native durability calls. No attribution to a particular disk or PostgreSQL
stall is made from these spans alone. Profiling overhead was independently
measured in [SP09a](sync-performance-sp09a.md#final-review), and remains included.

The screen is limited to the current two-table workload, batch sizes and storage
profile. It does not assess many wide tables, large joins, skewed/hot-key groups,
long soaks or alternative storage. It does not qualify EC2 instance scaling.
The archived historical overload is source-limited and cannot establish achieved
1,000 rows/s throughput. Only the eight-client qualified cells establish the
reported short-duration input rate.

No runtime, instrumentation, durability or scheduler setting changes in this
slice. Its measurable contribution is the gate decision, not another improvement.
Repeating the unchanged end-to-end suite would not measure a new candidate.

## Reopening the experiment

Reopen SP09b when request-level profiling on a relevant workload shows sustained
CPU-bound preparation or merge on the critical path, or explicitly amend its
scope to test durability-wait overlap. First qualify sufficient source load and
capture capacity so input does not mask apply scaling. Add at least four changing
tables so concurrency four is exercised, with balanced, skewed and hot-key cases.

Freeze independent concurrency 1/2/4 variants at matched affinity, memory, source
load and publication semantics. Measure each variant separately against the same
serial predecessor, including instrumentation controls. Keep one in-flight group
and one publication writer; expose an epoch only after every table is verified
and durable. Preserve per-table replay identity and source ordering. Reserve disk
and memory across the whole batch before tasks start, bound aggregate native
thread pools and process counts, and retain existing deadline/authority fences.

Before accepting any candidate, kill or fail one task while others finish, cancel
and revoke authority, restart the daemon, and prove earlier pinned epochs remain
readable with no partial publication. Measure safe replay, cleanup, RSS, storage
amplification, control-plane responsiveness and fairness. Keep concurrency only
if a matched throughput or preparation-latency gain survives total resource costs.
These are future acceptance requirements, not completed validation in this slice.

## Reproduction

```sh
python3 e2e/native/performance/parallelism_screen.py \
  docs/architecture/sync-performance-evidence/2026-10-02-sp09a-reviewed
python3 -m unittest discover -s e2e/native/performance -p test_parallelism_screen.py
```

The analysis launches no workloads and does not modify source evidence. Regression
checks verify that repeated same-process requests use their own CPU deltas, cold
and unsuccessful profiles are excluded explicitly, and altered receipt files are
rejected. Reproduction against the retained 18-trial archive passed.
