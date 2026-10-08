# SP08: observe durable capture before admission

Status: **measurements reviewed; keep the latency improvement; release CI pending**,
2026-10-01 UTC. [PR #132](https://github.com/supabricks/platform/pull/132) remains a
draft stacked on SP07. The [reviewed evidence](sync-performance-evidence/2026-10-01-sp08-reviewed/README.md)
contains 84 performance/control trials and six lifecycle fixtures. All final
trials are correct and fresh with clean teardown; no contention replacements
were needed. The older four-client overload workload still misses its offered
input. This is a latency improvement, not a source throughput gain.

## Measured results

Three fresh matched pairs per cell, with identical workload, instrumentation,
resources and packages except the native binary. Times below are medians of
trial p95s. Percentage changes are medians of the three paired changes, which
need not equal the percentage change between the two medians.

| Logical CPUs | Offered rows/s; clients | Predecessor p95 | Candidate p95 | Paired p95 change | Paired CPU change |
| --- | --- | --- | --- | --- | --- |
| 4 | 50; 4 | 2.221 s | 1.871 s | −16.67% | −0.37% |
| 16 | 50; 4 | 2.264 s | 1.894 s | −15.85% | +0.95% |
| 8 | 1,000; 4 | 2.994 s | 2.727 s | −8.92% | +0.51% |
| 16 | 1,000; 4 | 3.022 s | 2.722 s | −10.47% | −0.32% |
| 8 | 1,250; 8 | 3.746 s | 3.434 s | −7.84% | +0.07% |
| 16 | 1,250; 8 | 3.734 s | 3.451 s | −7.58% | +0.06% |

The four-client overload candidates achieve median 724/735 rows/s at 8/16 CPUs,
not 1,000. The source-qualified candidates achieve 1,247.241–1,249.031 rows/s at
8 CPUs and 1,249.558–1,249.824 at 16 CPUs. All twelve source-qualified main trials
meet the offered-input tolerance. Candidate p95 ranges are 3.434–3.453 s and
3.406–3.455 s respectively. Earlier SP06/SP07 source stalls remain unresolved
in #127; these healthy trials do not erase those outcomes or establish universal
8-core capacity. This is local same-host evidence, not EC2 qualification or an SLA.

At 1,250 rows/s, paired peak-memory changes are +0.04% / +1.22%; median CPU use is
approximately 1.37 / 1.64 cores. All 36 main trials pass correctness and freshness.
The 48 bridge/profiler-control trials also pass both gates; their historical
four-client overload input limitation remains explicit in the raw results.

## Attribution and resource checks

Commit-to-admission p95 falls about 13% at source-qualified load: median
2.140 → 1.852 s at 8 CPUs and 2.133 → 1.858 s at 16 CPUs. Admission-to-worker-start
p95 falls 8–10%; apply preparation and publication p95 change by less than 0.5%
there. Capture-observation upper bounds are effectively unchanged. The improvement
is consistent with using fresher durable progress during admission, without changing
capture reporting, deliberate micro-batching or worker lifetime. Stage percentiles
are separate distributions and cannot be added or interpreted as exact causal shares.

Successful apply-worker counts across whole source-qualified fixtures change from
median 183/184 to 186/186. These counts include setup and warmup; they are only
comparable within an unchanged profile. No empty-publication churn occurs in any
of the six lifecycle fixtures. Their median idle CPU use is 0.0795 → 0.0802 cores.
Status API p95 medians are 38.2 → 43.6 ms, with individual candidate p95s of
35.0–48.2 ms; p99 medians are 53.8 → 49.8 ms. Fifty correlated API samples per
fixture and three pairs do not establish a precise responsiveness effect. The
small absolute change does not outweigh the measured pipeline benefit.

The common refactor/profiler bridge passes its predefined screening bounds:
paired median input −0.012%, p95 +0.312%, CPU +0.218%, peak memory −0.504%.
At qualified load, enabling the final candidate profiler costs about 6.37% / 4.79%
CPU and 6.15% / 3.72% peak memory at 8/16 CPUs. Paired p95 changes are +0.011% /
−0.110%, and input changes −0.043% / −0.011%. The runtime comparison uses the
same profiler in both arms; instrumentation is not free. Individual trials,
ranges, stage distributions and all controls are in review-results.json.

## Final change and identities

The daemon ingests each capture receipt once before sync admission during normal
operation. The former order scheduled work before ingesting the current receipt,
so a newly admitted batch could omit already durable transactions and leave them
for the next publication cycle. The 200 ms daemon timer, 250 ms capture reporting,
500 ms minimum batch interval, one publication writer, fairness, authority and
revision checks, backpressure, cancellation and worker lifetime remain unchanged.
Read failures defer admission/dispatch but never suppress worker fencing. The
unchanged periodic tick provides recovery; no notification transport was added.
Early receipt ingestion does not run during native source teardown.

- Common predecessor native source: `26e2c902a8ac4448266d19936e17924927ebec8f`.
- Candidate native source: `54deabbf6ed105def5c3323b28a0df7b6e54bcc9`.
- Frozen controller: `9d1aa80b042e69a8cac60373aa8a288544d466e3`.
- Workload harness: `be4701cce5694ed00349ab3db9b577592965d7d7`.
- Both packages share the accepted SP04 payload except the native binary. Package
  identities, full inventory verification and actual installation-verifier receipts
  are retained. The common predecessor has the same capture observation profiler
  span and extracted phase, executed at the original point after admission.

## Validation, retained failures and merge readiness

207 local Rust tests and 73 accounting/package tests pass. The native capture
regression gate passes all ten checks. All six final continuous fixtures pass
bootstrap, atomicity, no-change idle behavior, pause/drain/resume, stale worker,
SIGKILL/restart, delayed controller, schema fencing and deletion. Every performance
receipt hash and reconstructed report/profile metric was checked during offline
review; each archive retains its own checksum inventory.

The [initial candidate failed shutdown/restart](sync-performance-evidence/2026-10-01-sp08-shutdown/README.md):
observing during source teardown persisted a terminal stream error as a resync
requirement (#133). The corrected candidate passes that boundary. A later
[package-preflight failure](sync-performance-evidence/2026-10-01-sp08-package-recovery/README.md)
revealed that diagnostic provenance had been added to a strict release manifest
(#134). Provenance is now external; package creation and campaign startup require
actual installation verification. Both stopped campaigns and their original
packages remain preserved. The final campaign used new immutable packages and
did not reset or relabel either failure.

On measured/controller head `9d1aa80`, native-cell and installed release-sync CI
pass on Linux and macOS, as do unit, portable and e2e checks. Two release checks
still fail: Linux environment lifecycle [#135](https://github.com/supabricks/platform/issues/135)
and governed data [#137](https://github.com/supabricks/platform/issues/137).
The macOS notebook rerun passes, but the earlier intermittent failure remains
tracked in [#136](https://github.com/supabricks/platform/issues/136). Their causes
and relation to SP08 are not established. Performance acceptance does not confer
complete release or merge readiness; the PR remains draft.

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
- Run local correctness tests and three matched pairs of native continuous lifecycle/fault checks,
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
continuous free-space guard. The common-refactor screen stops for review if any
trial fails input/correctness/freshness, or paired median source throughput falls
more than 5%, p95 lag rises more than 5%, or CPU/peak memory rises more than 10%.
These are screening bounds, not statistical equivalence claims. Do not enable the optional SP07 host I/O sampler.

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
