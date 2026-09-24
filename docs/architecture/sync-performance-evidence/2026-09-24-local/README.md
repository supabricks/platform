# Local CPU scaling evidence — 2026-09-24 UTC

[Interpretation and methodology](../../sync-core-scaling.md) ·
[Benchmark PR #89](https://github.com/supabricks/platform/pull/89)

This is shared-host screening of a private diagnostic installation, not EC2
emulation, a maximum-capacity claim, or qualification of a signed release.

The primary matrix ran from **01:53:36 to 02:54:30 UTC**: 27 sequential trials,
three repeats of 4/8/16 logical CPUs × 50/250/1,000 offered changed rows/s, fixed
16 GiB memory, and 45 seconds of measured load when warmup succeeded. Twelve
trials completed full transaction attribution and source-equality checks;
15 recorded runtime failures. All 27 had clean owned-process teardown.

An external mutation-test build began around 02:37:50 UTC and was still active
after the matrix. Trials 23–27 overlapped its activity and are flagged as
potentially affected by host contention. They remain in every full-matrix
statistic. The process observations do not establish the exact contribution of
that build to each trial's latency. A quiet-host repeat is tracked in
[#90](https://github.com/supabricks/platform/issues/90).

## Files

| File | Contents |
| --- | --- |
| [matrix.json](matrix.json) | Runtime/image hashes, topology, limits, filesystem, randomized order, all trial outcomes and controller resume history |
| [raw-reports.json.gz](raw-reports.json.gz) | Complete primary manifest and every trial/cleanup JSON report, including sampled backlog and raw resource counters |
| [trials.csv](trials.csv) | One row per primary trial; failures and unavailable statistics remain explicit |
| [summary.json](summary.json) | Counts and median/minimum/maximum of available trial statistics, with sample counts for each statistic |
| [scaling.png](scaling.png), [scaling.svg](scaling.svg) | Standalone chart exports, including slow runs and failure counts |
| [host-contention.json](host-contention.json) | Bounded, read-only host observations used to flag the late interval |
| [worker-failure-codes.json](worker-failure-codes.json) | Retained state confirms all nine resync failures had the generic `incremental_worker_failed` code |
| [drain-timeout-evidence.json](drain-timeout-evidence.json) | Stopped-spool sequence counts independently prove incomplete capture in all six primary drain timeouts |
| [development-attempts.json.gz](development-attempts.json.gz) | The separate 15-second pilot and three stopped harness-development attempts; excluded from primary statistics |
| [diagnostic-report.json.gz](diagnostic-report.json.gz) | One supplemental exception-instrumented trial; excluded from primary statistics |
| [SHA256SUMS](SHA256SUMS) | Integrity checks for the files in this evidence directory |

Compressed reports contain JSON, not source database files or private service
logs. Fixture data is synthetic. No credentials or daemon configurations are
included. Successful fixtures were removed; failed fixture logs/data remain
local for diagnosis after their owned runtimes stopped.

## Provenance and interpretation

The baseline platform binary is from
`a8fd376536d3d6c5198df0badb6ee13cfaa6702f`; the package manifest retains its original
build provenance and dirty-package designation. The exact identities are:

- Binary SHA-256: `a521c26a3c12c559e3b2cdce8cc946b631378772f52cd61bec332ddeb85abff9`.
- Package manifest SHA-256: `78928698010df68ad72717b042728148abcb48a01774890ff3efdd6af3d9bb48`.
- Qualification image: `sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec`.

The primary worker and accounting-test hashes match
[commit 689f16a](https://github.com/supabricks/platform/commit/689f16a).
The controller initially stopped after trial 6 because the cleanup wrapper
normalizes a nonzero child result to exit code 1. The controller was corrected to
check the original child result in `cleanup.json`; its new hash and continuation
are recorded in `resume_history`. Trial code, runtime, workloads, and order did
not change. No primary trial was overwritten or repeated during that resumption.

The earlier development attempts retain an observer lock timeout and runtime
failures that the initial runner did not yet classify separately. The final
observer closes read connections promptly, retries only known SQLite busy/locked
conditions, counts those events, and still rejects missing transaction markers.
Development attempts use different harness revisions and are not pooled with
the primary matrix.

After the primary matrix, the harness gained a conservative timeout guard: if
commit markers are missing and the durable sequence count cannot prove capture
is incomplete, the trial is an invalid measurement rather than a runtime
capacity result. For the archived six timeout cases, post-stop inspection proves
at least 3,460–8,792 source commits were still uncaptured. Spool pruning retains
the newest sequence anchor, so sequence numbering does not restart. Warmup and
measured commits alone require more sequence entries than existed, even allowing
for the captured control/barrier transactions. Thus the original timeout outcomes
are corroborated independently of possible gaps in the observer's history.

The supplemental diagnostic changed only exception logging in a private copy of
`incremental_worker.py`, recording exception type, SQLite error code and stack
locations after an exception occurs. It preserved the baseline package, binary,
and production failure behavior. That 4-CPU/250-row/s attempt ran during ongoing
host contention, achieved 217.054 rows/s and hit the drain timeout. It emitted no
incremental exception record, so it did **not** determine the cause of the generic
worker failures in [#88](https://github.com/supabricks/platform/issues/88).
The exact instrumentation text and both file identities are in its report.

Summary p95/p99 values are statistics of trial percentiles, not pooled transaction
percentiles. `measured` means complete attribution and final data equality; a
measured trial can miss the latency target. Runtime failures have no complete
latency distribution. Warmup failures also have no measured-load CPU statistic.
The CSV derives achieved-input success independently of replication success.

## Reproduce the summaries

From the platform repository root:

```sh
python3 e2e/native/performance/summarize.py \
  docs/architecture/sync-performance-evidence/2026-09-24-local
```

Add `--plot` in an environment with Matplotlib to regenerate PNG/SVG exports.
The committed charts were generated with Python 3.12 and Matplotlib 3.11.2.
The summarizer validates package/binary identity, actual affinity, memory,
disabled swap, absence of CPU quota, complete trial counts, and process cleanup.
The [harness instructions](../../../../e2e/native/performance/README.md) describe
running a new matrix. Use a quiet host for a clean follow-up.
