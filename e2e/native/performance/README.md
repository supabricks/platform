# Local CPU scaling benchmark

This experiment measures the installed PostgreSQL → analytical publication path.
It does not qualify a release, change SY08 gates, or emulate an EC2 instance.
See [the methodology and results](../../../docs/architecture/sync-core-scaling.md).

Run from the platform repository on a Linux host with Docker, cgroup v2, `lscpu`,
and `findmnt`. Supply an existing installed package and a Docker image containing
the native qualification dependencies (Python, psycopg, boto3, and psutil). The
default image is the local `supabricks-sy08-qualifier:latest`; the controller pins
its immutable image ID before starting. This command does not build that image.

```sh
python3 e2e/native/performance/matrix.py \
  --release /absolute/path/to/supabricks \
  --runtime-revision FULL_SOURCE_COMMIT \
  --output /absolute/path/to/new-results-directory
```

Defaults: 4/8/16 logical CPUs, 16 GiB memory with swap disabled, 50/250/1,000
changed rows/s, three repetitions, 45 seconds of measured load per trial, four
source clients, and 10,000 rows in each of two tables. Override with `--cpus`,
`--memory-gib`, `--rates`, `--repeats`, `--seconds`, `--clients`, or `--rows`.
The CPU counts must fit the host and select complete SMT sibling groups. Trials
run sequentially in seeded randomized order; do not run other benchmarks beside
them, and avoid concurrent builds or other disk-intensive work. Allow roughly an hour for the default matrix, including failed-trial drain
timeouts. The host and its other applications remain running.

Each trial starts a disposable native stack in its own network-isolated container,
mounts the runtime and repository read-only, and writes only its new output and
scratch directories. Container memory includes the source, storage services,
catalog, capture, apply, and measurement processes. CPU affinity is the restriction;
there is no CFS CPU quota. This tests the whole stack sharing the selected CPUs.

The controller refuses an existing output directory unless `--resume` is supplied.
Resume validates completed outcomes and unchanged trial code, workload, runtime,
and image, then records the controller hash in resume history. It does not rerun
or overwrite completed trials, and refuses to continue after a measurement or
cleanup error. A nonblocking directory lock prevents concurrent controllers.
It records script hashes,
runtime and package hashes, image ID, CPU topology, memory limits, filesystem,
trial order, and per-trial cleanup. **Do not edit the scripts or package during a
matrix.** A locally rebuilt package must be described as such; a revision argument
is an operator assertion, not proof of signed release provenance.

The private trial log and scratch directory can contain native service details.
Retain them locally when diagnosing failures; publish the structured reports,
not credentials, daemon configurations, or private logs. Successful trial scratch
directories are removed. Failed fixtures are retained after stopping their owned
runtime. `catalog_gate.py` independently checks descendant cleanup; the controller
stops on any measurement or cleanup error.

Report interpretation:

- `measured`: every measured transaction was attributed to its first covering
  publication, and both published tables matched the frozen PostgreSQL source.
  `within_5s_p95` and `offered_load_met` independently report latency and load
  misses; a valid measurement is not necessarily a passing performance result.
- `runtime_failed`: the policy reported a failure or did not drain within 120
  seconds. There is no complete latency percentile or final correctness pass.
  The matrix continues only after clean teardown, preserving that failure.
- `error`: measurement, setup, attribution, or correctness failed. The matrix
  stops; diagnose the private log before starting a new output directory.

The observer retries short SQLite lock conflicts, counts them, and rejects missing
transaction markers. It never disables production spool pruning or holds a reader
transaction across samples. Monitoring adds overhead; measurements include it.
After a drain timeout, the runner checks the stopped spool's sequence against
the number of source commits. Missing observation history is a measurement error
unless that conservative count proves capture is still incomplete. When every
measured commit marker is known, publication timeout can be classified directly.

Run the accounting tests in the same qualification image:

```sh
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD:/repo:ro" -w /repo supabricks-sy08-qualifier:latest \
  python3 -m unittest discover -s e2e/native/performance -p 'test_*.py' -v
```

Summarize a completed output directory or an unpacked committed evidence directory:

```sh
python3 e2e/native/performance/summarize.py /path/to/results
```

This validates package identity, affinity, memory/swap/quota settings, trial
completeness, and cleanup before writing `trials.csv` and `summary.json`. It accepts
the archived `raw-reports.json.gz` format as well as individual trial directories.
Add `--plot` in an environment with Matplotlib to produce standalone PNG/SVG
charts. Failure counts remain visible, and the summary reports the number of
available observations for each statistic. A source input-rate result is recorded
independently of replication success.

## Whole-workflow profiling (Linux diagnostic packages only)

Build a separate opt-in binary and native sync-call probe, then create a new
package. The original package must remain available and unchanged:

```sh
cargo build --release -p supabricks-local --bin supabricks --features sync-profile
cc -O2 -shared -fPIC -Wall -Wextra -Werror \
  e2e/native/performance/profile_io.c -ldl -pthread -o /tmp/profile_io.so
python3 e2e/native/performance/profile_package.py \
  --base /absolute/path/to/baseline-package \
  --output /absolute/path/to/new-diagnostic-package \
  --binary target/release/supabricks --io-library /tmp/profile_io.so
python3 e2e/native/performance/matrix.py \
  --release /absolute/path/to/new-diagnostic-package \
  --runtime-revision FULL_INSTRUMENTATION_SOURCE_COMMIT \
  --output /absolute/path/to/new-profile-results --profile
```

`profile_package.py` breaks hardlinks before changing the binary, three worker
entry points, or the Python launcher; it adds the Python and native probes and
updates the private inventory. It verifies the baseline files are unchanged and
records before/after hashes. This is a diagnostic installation, not a signed or
qualified release. Without the Cargo feature, daemon spans compile to no-ops.
Even in a diagnostic package, profiling requires the disposable fixture's
`sync-profile/enabled` marker, created only by `--profile`.

Coverage:

- Exact client timings for BEGIN, the existing two-table UPDATE, transaction-ID
  lookup, and COMMIT, without changing the source SQL or client count.
- PostgreSQL wait-event samples every 200 ms; WAL statistics and owned-process
  CPU, RSS, I/O, and context-switch samples approximately once per second.
- Safekeeper/pageserver WAL, flush, and write counters/histograms about once per
  second, with identifying labels removed and endpoint availability recorded.
- Capture socket wait/receive, decode, source checks, durable spool append,
  SQLite statement classes, progress/status writes, feedback, and pruning.
- Native `fsync`/`fdatasync` counters and elapsed time in Python workers, including
  SQLite C calls. Per-COMMIT counters distinguish native sync time from other
  commit time. The preload forwards each call and preserves its return/errno;
  it does not relax synchronous settings, skip syncs, or record file paths.
- Worker imports, journal reading, planning, filesystem budget scans, checksums,
  Delta merge, durability, inventories, and maintenance. Fixed work counters
  include captured transactions/bytes, batch input, and Delta operation metrics.
- Daemon scheduling, capture/apply dispatch, publication verification, file sync,
  descriptor writes, and atomic SQLite publication commit. Existing durable
  records retain batch admission/start and preparation/publication timestamps.
- Safe exception types, SQLite codes, and stack function/file/line information.
  Full apply exception text stays in the private worker log and is not archived.

`profile.json.gz` retains snapshots and counters even when replication fails.
Profiles use fixed labels and omit SQL text, source rows, credentials and
exception messages. Diagnostic output is not fsynced and is limited to 8 MiB per
worker. Missing required streams, native hooks, monitor errors, malformed output,
write failures or budget exhaustion invalidate measurement rather than silently
supplying a partial profile. The daemon emits a final snapshot; workers terminated
by the supervisor can have an incomplete tail, explicitly visible through their
last timestamp and `final` flag. A snapshot is attempted before worker receipts.
Per-process counters start at process launch and include setup and shutdown;
analysis must select the stated phase/window rather than label lifetime totals
as load-only work. Short processes can fall between OS samples.
An isolated process `AccessDenied` sample is retained as an explicit omission;
denial for the daemon, three consecutive denials for one PID, or over 100
omissions invalidates profiling. This tolerates Linux process-exit races without
silently accepting a persistent permission problem. Monitor failures retain
partial structured observations for diagnosis, but remain invalid trials.

Python histograms use power-of-two microsecond buckets; report their bounds,
not exact percentile estimates. Span totals are inclusive and overlap; `self_ns`
subtracts instrumented child spans on the same thread. Never add parent/child
wall times or interpret overlapping processes as a single elapsed duration.
Native sync time is contained in SQLite/durability spans, not an additional stage.
Measured profiler write/monitor time is retained, but does not include every
probe overhead. Use paired profiling-on/off controls with the same diagnostic
package and host monitoring; controls estimate activation overhead, not the cost
of diagnostic code imported in both modes. Keep controls separate from the
requested 12-trial matrix and preserve failures and all contended attempts.

## Paired slice comparisons (SP00 onward)

`compare.py` runs twelve candidate trials and twelve fresh predecessor trials.
Its default explicit cells are `4:50,16:50,8:1000,16:1000`, each repeated three
 times. Pair order is seeded; each cell includes both predecessor-first and
candidate-first trials. `matrix.py --cells` also accepts this explicit list.

```sh
python3 e2e/native/performance/compare.py \
  --slice SP00 --hypothesis 'Unchanged runtime; old versus supported harness' \
  --predecessor-release /absolute/diagnostic-package \
  --candidate-release /absolute/diagnostic-package \
  --predecessor-revision FULL_RUNTIME_COMMIT \
  --candidate-revision FULL_RUNTIME_COMMIT \
  --predecessor-harness /absolute/clean-old-checkout \
  --candidate-harness /absolute/clean-new-checkout \
  --baseline docs/architecture/sync-performance-evidence/2026-09-24-workflow-profile \
  --output /absolute/new-comparison-directory
```

Both harness checkouts must be committed and clean. The runner freezes their
revisions and tracked native qualification/installer Python hashes, runtime
binary and package manifest hashes, immutable container image, workload,
affinity, memory and filesystem. It rechecks identities before and after each
trial. Runtime source revision is still an operator assertion. Use the same
profiling activation on both arms. Changed instrumentation needs separately
recorded activation controls before attributing a runtime improvement.

Before every trial, the host must have five rolling quiet minutes. Five-second
samples record CPU, memory, disk/pressure counters, optional frequency/thermal
sensors, and known build process identities and their descendants' activity.
Idle build parents do not prevent progress. This is a shared-host observation,
not exclusive host ownership: short processes between samples and unknown build
tools can escape detection. Do not intentionally start other intensive work.
Each JSONL segment is limited to approximately 16 MiB, with a hard 256 MiB total
budget; exhaustion, read failures or sampling gaps stop measurement. No external
process is signaled. Qualifier children alone are stopped on interruption.

A build overlapping either arm invalidates the entire pair. Both arms remain in
`attempts`, and both are repeated after another quiet interval. Runtime failures
remain accepted outcomes. Measurement, malformed evidence or cleanup failures
stop the series. `--resume` requires identical inputs and validates all accepted
receipts and artifact checksums. It refuses an interrupted active trial because
cleanup is uncertain. An interruption safely between trials retains and replaces
the incomplete pair, consuming the same bounded attempt budget.

`experiment.json` retains every attempt and quiet receipt. `comparison.json`
reports pairwise changes, failure counts, trial medians/ranges and observation
counts against both the fresh predecessor and the checksum-verified original
archive. Failed trials never supply complete latency. Aggregate latency changes
are suppressed if either group contains failures. Source input shortfalls are
separate from replication outcomes. Component metrics may have fewer observations:
capture rates use differences between cumulative snapshots strictly inside the
load window; apply medians use only successful workers started in that window.
They do not represent failed workers or complete end-to-end latency.

Recompute and export evidence without restarting any runtime:

```sh
python3 e2e/native/performance/report_comparison.py /absolute/results
python3 e2e/native/performance/archive_comparison.py \
  /absolute/results docs/architecture/sync-performance-evidence/NEW-DIRECTORY
```

Export preserves the manifests verbatim, including local installation paths,
and copies structured receipts/profiles and host samples with SHA-256 checksums.
It excludes private logs and scratch. Retain private output locally for diagnosis.
Nondefault pilot dimensions are labeled `standard_matched_protocol: false`.
A completed runner is not an automatic performance approval: document the result,
its limitations and a retain/revise/revert decision after each logical slice.
See [the measurement contract](../../../docs/architecture/sync-performance-comparisons.md)
for long-run profiling design and counter requirements.
