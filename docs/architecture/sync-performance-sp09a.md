# SP09a — Bounded incremental worker reuse

Status: **reviewed; keep bounded worker reuse**, 2026-10-02 UTC. All 84 accepted
performance/control trials, six lifecycle fixtures and latest CI passed.
SP09b table parallelism remains a separately gated experiment.

The initial candidate `a8384f0` passes 206 Rust tests (four ignored), 100 Python
tests, 74 harness tests and a full-stack lifecycle screen. Three successive epochs
used PID 1181; killing that assigned worker recovered correctly. Idle retirement,
pause/resume, capture/daemon recovery, schema fencing and resync passed, with zero
leaked descendants. This screen has no quiet-host performance qualification claim;
[receipts and package proofs](sync-performance-evidence/2026-10-01-sp09a-screen/validation.json)
are retained. The measured paired campaign is now complete; see the review below.

## Why test reuse

The [retained startup screen](sync-performance-evidence/2026-10-01-sp09a-screen/startup.json)
uses the 18 candidate main trials from SP08. Successful workers started during
measurement have per-trial median import times of 201–219 ms. At qualified load,
imports take 208–216 ms alongside 360–367 ms of apply work. These are direct
instrumented spans, not a subtraction of unrelated percentiles. They establish a
material opportunity, not an estimate of end-to-end improvement. Failed or
incomplete requests remain explicitly counted. The source profiles are hashed
and the screen is reproducible with `e2e/native/performance/reuse_analysis.py`.

## Candidate

Retain at most four serial workers per daemon. Each worker is bound to one capture
identity, service authority/policy revision, source revision, daemon generation,
storage generation, and installed Python/worker paths. Excess captures retain the
existing short-lived process behavior. There is no request queue or table
parallelism. Every dispatch and receipt still passes `incremental_live`, including
current run/epoch, source and authority validation. Idle workers are fenced on
policy, source, capture or runtime changes too.

Private, atomically written mailboxes carry the existing bounded request config.
Request IDs and attempt numbers distinguish retries. A completion marker follows
release of request-local handles and mutation leases; the daemon does not consume
a live worker's result before this marker. All Delta tables, journal connections,
plans and inventories are constructed anew for each request. Only imported code
and native library runtime state are reused. No credential cache is added.

The daemon retires workers after 64 requests, 60 seconds of age, five idle seconds,
or 512 MiB between requests. Python independently stops at 65 seconds / six idle
seconds, leaving a margin for the 200 ms daemon tick. The existing 768 MiB active
RSS limit, run deadlines, disk/row/value limits and bounded retry protocol remain.
Failure/defer recycles the worker. Cancellation kills its owned process group;
daemon recovery fences all former processes and discards their mailboxes. Durable
plans/results remain the existing crash-recovery authority. Publication still has
one writer and only exposes a complete verified group.

## Measurement contract

Freeze source revisions, packages, profiler, controller, workload, image and
configuration before starting the campaign. The SP08 accepted candidate package
is the runtime predecessor. The common arm changes only diagnostic instrumentation
to emit one stream per request, identified by process ID, request ID and attempt.
Cold import timing is retained for the first request; subsequent requests report
warm apply timing. CPU/native counters labeled process-cumulative must never be
summed repeatedly across requests. Whole-stack cgroup accounting remains primary.

Before the main comparison:

- Run Rust store/fencing tests, Python real-Delta reuse/replay and mailbox boundary
  tests, and diagnostic attribution tests.
- Run three balanced fresh lifecycle fixture pairs, including idle CPU/status
  latency, same-process successive epochs, an in-flight worker kill, idle retirement,
  pause/drain/resume, capture/daemon recovery, schema fencing and cleanup.
- Run three qualified 8-CPU pairs comparing the accepted package to common
  instrumentation, then three common profiler activation pairs. Retain and review
  drift; do not count it as a runtime benefit.

Then run 24 historical main trials (three pairs each at 4/16 CPUs and 50 rows/s,
8/16 CPUs and 1,000 offered rows/s; four clients, 45-second measurement), followed
by 12 qualified main trials (three pairs at 8/16 CPUs, eight clients, 1,250 offered
rows/s, 60-second warmup and 300-second measurement). Run 36 final candidate
profiler activation controls over these same cells.

Use whole SMT affinity, 16 GiB, no swap/CPU quota, sequential fresh stacks, a
300-second build-quiet interval and the existing bounded whole-pair contention
replacement rule. Source stalls and runtime failures remain outcomes. Measurement,
identity or cleanup failures stop the campaign. Freeze a new campaign after any
runtime or instrumentation fix; retain failed attempts.

Review matched pipeline latency, offered-load attainment, CPU/RSS, cold/warm
request tails, process recycling, memory growth, idle cost and lifecycle outcomes.
Keep reuse only for demonstrated net benefit without a lifecycle or authority
regression. Otherwise retain short-lived workers. A passing startup screen alone
does not complete SP09a.

## First campaign: diagnostic packaging failure

Campaign 01 passed six lifecycle fixtures, six accepted/common instrumentation
trials and six common profiler activation trials. It stopped during the first
candidate historical trial: the native binary had been compiled without
`--features sync-profile`, so `daemon.jsonl` was absent. Capture and per-request
apply profiles were present. The collector correctly rejected incomplete workflow
instrumentation; cleanup recorded zero leaked/remaining descendants. The trial is
retained as invalid measurement, not a performance gain or a runtime failure.

[Retained failure and recovery evidence](sync-performance-evidence/2026-10-01-sp09a-diagnostic-recovery/failed-campaign.json)
includes the invalid trial, partial profile and cleanup receipt. Issue #139 tracks
the admission gap. A new behavioral preflight runs an exact binary copy as an
engine-free daemon, enables profiling, shuts down, and requires a final daemon
profile. It rejects the original binary and accepts the corrected feature-enabled
build. Campaign 02 will rerun all phases with new immutable package identities;
no frozen package or incomplete campaign is edited/resumed.

On head `53b67c0`, all required CI and both installed sync gates passed. The macOS
catalog browser gate aborted with PermissionError during descendant census before
producing its report/cleanup receipt (#140); Linux catalog passed. This separate
qualification failure remains unresolved. The previously observed #135/#137
release gates passed on this head without a targeted fix here.

## Controller interruption and evidence-preserving continuation

Campaign 02 completed its six lifecycle fixtures, twelve instrumentation/control
trials, all 24 historical main trials, and one qualified pair. Its controller and
host monitor then disappeared without updating the last `running` checkpoint.
The last host sample was 2026-10-01 21:24:01 UTC. An independently launched trial
finished afterward with clean teardown, but lacked continuous host monitoring and
was not accepted. The cause of controller loss is unconfirmed (#141). Status-file
contents alone are insufficient evidence of a live campaign.

Recovery preserves the original campaign and all its files. A separate
continuation copies the qualified phase, verifies every retained accepted receipt
and artifact hash, retains its first accepted pair, and repeats the interrupted
pair after verifying cleanup. It uses the same frozen comparison controller,
packages, workload, pairing order and limits. Completed phases are not rerun.
The final historical and qualified profiler controls follow. The new outer
supervisor records its own hash and a ten-second heartbeat and runs as a systemd
user service; failures stop for investigation rather than automatically retrying.
Future status checks must inspect service state and heartbeat age as well as the
phase ledger. Runtime and instrumentation settings are unchanged.

## Final review

[Reviewed evidence](sync-performance-evidence/2026-10-02-sp09a-reviewed/README.md)
contains all 84 accepted trials and six lifecycle fixtures, including the original
completed phases and supervised continuation. Every retained raw receipt hash and
metric was revalidated with the unchanged frozen controller. The continuation
exited successfully at 2026-10-02 03:39:51 UTC. The unmonitored pair is excluded;
its full replacement retained the original pairing protocol. No runtime failure
or correctness/freshness failure occurred among the accepted trials.

At 1,250 offered rows/s, eight clients, 16 GiB and 300-second measurement:

| Logical CPUs | Actual candidate rows/s (median) | p95 lag predecessor → candidate | Paired p95 change | Paired CPU change | Paired peak memory change |
| --- | ---: | --- | ---: | ---: | ---: |
| 8 | 1,249.222 | 3,402.798 → 2,995.737 ms | −11.96% | −25.67% | +0.35% |
| 16 | 1,249.783 | 3,411.920 → 3,031.190 ms | −11.33% | −40.51% | +1.58% |

Three matched pairs per cell meet input and five-second p95 requirements. At
historical offered 1,000 rows/s the lag gain is 17.72% / 17.32% at 8/16 CPUs,
but four clients achieve only about 732–749 rows/s; this is not a 1,000 rows/s
throughput qualification. At 50 rows/s p95 changes −1.97% / +0.37% at 4/16 CPUs;
CPU decreases 47.63% / 72.40%, with paired peak memory increases below 5%.

Instrumentation bridge drift is small: p95 +0.125%, CPU +0.146%, source +0.003%,
peak memory −1.117%. Common profiler activation adds 5.29% CPU and 6.43% peak
memory with +0.01% p95. Candidate qualified activation adds 7.86% / 9.13% CPU
and 5.62% / 7.23% peak memory at 8/16 CPUs, with p95 +0.018% / −0.337%.
The historical 16-CPU/50-row profiler control increases p95 5.27%; profiling is
not free or universally neutral. These costs remain explicit and are not
subtracted from the matched main comparisons. Unprofiled candidate qualified
p95 medians are 3,013.525 / 3,024.294 ms at approximately 1,250 rows/s.

All six lifecycle fixtures pass with clean teardown. Median whole-stack idle CPU
is 0.08569 → 0.08720 cores; status API p95 is 45.020 → 36.013 ms, p99
48.199 → 49.512 ms. Candidate fixtures verify actual same-process successive
epochs, in-flight kill recovery and idle retirement, plus existing authority,
pause/restart/schema fencing and pinned-reader behavior. Bounded recycling and
these short runs do not establish long-soak memory behavior; SP11 remains required.

Keep reuse for net latency and CPU benefit without an observed lifecycle regression.
The latest implementation/recovery head 27270fe passed all 47 CI jobs, including
both installed sync gates and the previously failing Linux probe/macOS catalog
gates. Their prior intermittent failures remain tracked in #110/#140; a passing
rerun does not establish a targeted fix. This is a local short-duration qualification,
not a universal capacity promise or completion of SP11/SP12.
