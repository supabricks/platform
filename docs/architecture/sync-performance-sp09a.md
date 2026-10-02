# SP09a — Bounded incremental worker reuse

Status: implementation and qualification in progress. No measured improvement or
release-readiness claim yet. SP09b table parallelism is a separate experiment.

The initial candidate `a8384f0` passes 206 Rust tests (four ignored), 100 Python
tests, 74 harness tests and a full-stack lifecycle screen. Three successive epochs
used PID 1181; killing that assigned worker recovered correctly. Idle retirement,
pause/resume, capture/daemon recovery, schema fencing and resync passed, with zero
leaked descendants. This screen has no quiet-host performance qualification claim;
[receipts and package proofs](sync-performance-evidence/2026-10-01-sp09a-screen/validation.json)
are retained. The measured paired campaign remains pending.

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
