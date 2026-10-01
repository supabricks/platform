# SP08: observe durable capture before admission

Status: implementation and qualification in progress; no performance decision.

The accepted SP04 runtime, retained through SP07, schedules continuous work before
ingesting capture receipts. Its daemon sleeps 200 ms after each maintenance turn;
capture reports progress every 250 ms and continuous policy admission retains a
500 ms minimum interval. Selecting the previous receipt can exclude newly durable
transactions from a batch, leaving them to wait for the next publication cycle.

An accepted SP07 eight-core candidate trial (`01-attempt03-candidate`) recorded
p95 commit-to-admission 2,115.464 ms, admission-to-worker-start 110 ms,
worker-start-to-prepared 636 ms, prepared-to-publication 1,075 ms, and end-to-end
3,705.219 ms. These distributions overlap and must not be added. The admission
interval includes deliberate batching and the previous active run, not just
scheduler delay. Capture observation's 112.112 ms p95 is an observer upper bound,
not the daemon receipt age. These numbers motivate an experiment, not a claim
that moving one phase removes two seconds of latency.

## One runtime variant

Move receipt ingestion ahead of sync admission within the same maintenance turn.
Read and persist each capture status once; leave capture lifecycle dispatch after
storage readiness checks. Retain the receipt identity, worker generation and
timestamp checks. A read failure defers admission and native dispatch while
cancellation/deadline/authority fencing still runs. The next periodic turn retries
from durable state. No new notification transport or recovery dependency is added.

Keep the 200 ms timer, 250 ms capture reporting, 500 ms batch interval, one
publication writer, admission ordering, source checks, budgets and worker lifetime.
There is no batch-interval experiment in this variant. Worker reuse and table
parallelism remain SP09 work.

## Frozen comparison protocol

Use persistent `build/sp08-20261001/` packages, checkouts, logs and raw receipts.
Both packages share every payload file except the native binary. Both use the
same capture observation/dispatch phase split and `capture.observe` profiler span;
the predecessor ingests receipts at the original point after sync admission.
The accepted SP04 package remains immutable and is the historical reference.

Before performance acceptance:

- Verify the common refactor/profiler against the accepted runtime: three matched
  pairs at 8 CPUs, 8 clients, 1,250 rows/s, 60 s warmup, 300 s measurement. Also
  run three profiling-off/on pairs of the common predecessor at this load.
- Run local correctness tests and native continuous lifecycle/fault checks,
  including stale worker, daemon restart, controller delay, pause, schema fence,
  source atomicity and idle no-change behavior. Record idle CPU and control API
  latency under the unchanged observation cadence in both arms.
- Run the mandatory 24 trials: three predecessor/candidate pairs at 4CPU:50,
  16CPU:50, 8CPU:1000 and 16CPU:1000, four clients, 45 s measurement and 5 s
  baseline/warmup.
- Run 12 source-qualified trials: three pairs each at 8 and 16 CPUs, eight
  clients, 1,250 rows/s, 60 s warmup and 300 s measurement.
- Run candidate profiler activation controls at all four historical cells and
  both source-qualified cells, three pairs each. Keep each configuration separate.

Use the pinned SP06 workload harness and qualifier image, 16 GiB/no swap, whole
SMT affinities, seeded balanced order and sequential fixtures. Require five build-
quiet minutes per measured trial, keep and repeat whole contended pairs within
the existing replacement bounds, retain runtime failures, and stop on measurement
or cleanup failure. Require at least 64 GiB free at admission; this is not a
continuous free-space guard. Do not enable the optional SP07 host I/O sampler.

Report individual trials, paired changes, CPU, peak memory, apply count and all
existing latency stages. Native `capture.dispatch` excludes receipt observation
in both new packages, so it cannot be compared directly with the old combined
span. `capture.observe` isolates the moved work; total daemon and end-to-end
metrics remain the performance decision. Independently measured stage percentiles
do not quantify exact causal shares of intentional wait, queueing or process launch.

Keep the change only if admission/end-to-end lag improves at matched actual input,
all correctness/freshness gates pass, and idle/resource/API measurements show no
material regression. Retain source stalls as outcomes rather than replacing them
because they do not match the hypothesis. A failed screen defers or rejects this
variant; it does not justify combining a second tuning change into these trials.
