# Second matched synchronization run — 2026-09-24 UTC

[Main report](../../sync-core-scaling.md) · [First matched run](../2026-09-24-quiet-followup/README.md)

The user requested another run of the same 12-trial follow-up. This archive
retains that new run separately, without replacing or pooling prior results.
Trials ran sequentially from 14:58:04 to 15:20:52 UTC.

| CPUs | Offered rows/s | Complete / trials | Trial p95 median (range), seconds | Failure count |
| --- | --- | --- | --- | --- |
| 4 | 50 | 3 / 3 | 3.915 (3.560–4.061) | 0 |
| 16 | 50 | 3 / 3 | 3.773 (3.636–4.688) | 0 |
| 8 | 1,000 | 0 / 3 | Unavailable | 2 resync, 1 drain timeout |
| 16 | 1,000 | 0 / 3 | Unavailable | 3 resync |

All six 50-row/s trials met input rate, passed the five-second p95 gate, and
passed full two-table equality against frozen PostgreSQL. Their average CPU use
was 0.750–0.764 cores with 4 CPUs available and 1.170–1.230 with 16 available.
The p95 target does not bound every individual transaction's lag.

Of the overload attempts, one 8-CPU resync failure was detected during warmup;
four resync failures were detected during drain after the measured load. All five
stopped fixtures recorded `incremental_worker_failed`. The remaining 8-CPU trial
timed out after 120 seconds of drain. It required at least 16,307 source commits
to be captured, but its stopped spool's highest sequence was 7,883, confirming
at least 8,424 uncaptured commits even when control transactions are counted.
Its observer also lacked 8,525 measured transaction markers. The raw report
retains this proof under `drain_timeout_evidence`.

The five overload trials that reached measurement accepted 652.824–815.576
changed rows/s. All six source-only baselines remained below the offered load,
at 858.852–885.843 rows/s. These are source input measurements, not successful
replication throughput. Failed workers may reduce active replication work during
the load window, so their CPU/rate figures cannot establish useful scaling.
All 12 attempts passed owned-process cleanup with zero leaked or remaining
descendants. No failed trial supplies a complete latency percentile.

## Unchanged workload and provenance

- Same four profiles as the first follow-up: 4/16 CPUs at 50 rows/s and 8/16 CPUs
  at 1,000 offered rows/s, three repeats each. The two load groups and randomized
  order within each group are unchanged (seed `20260923`).
- Same complete SMT sibling affinities, 16 GiB memory, disabled container swap,
  four source clients, two 10,000-row tables, 45-second measured load, default sync
  policy, bounded drain, full-table checks, inventory verification, and fresh
  disposable fixtures. No runtime changes or exception instrumentation.
- Runtime revision: `a8fd376536d3d6c5198df0badb6ee13cfaa6702f`.
- Binary SHA-256: `a521c26a3c12c559e3b2cdce8cc946b631378772f52cd61bec332ddeb85abff9`.
- Package manifest SHA-256: `78928698010df68ad72717b042728148abcb48a01774890ff3efdd6af3d9bb48`.
- Image: `sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec`.
- Harness revision: `e049ec29ff5ec61af43bfb3d18e966625dcb8962`, clean during all
  trials. All harness Python file hashes match the first follow-up. The commit
  difference contains that earlier run's documentation and evidence only.

Every trial now uses a one-trial matrix; the first follow-up used one six-trial
matrix for low load. `followup.json` records the outer repeat identity, while each
individual matrix uses repeat `1`. Aggregate CSVs preserve both identities and
link to their exact source manifest. This orchestration change does not alter
the in-container workload or trial code.

## Host monitoring and inclusion rule

The predeclared rule required five minutes without active external build samples
before each trial, retaining and repeating any overlapped attempt regardless of
performance. A short compiler job delayed initial startup; no active external
build was observed during any of the 12 trials, so no retries or exclusions were
needed. The first follow-up required only one quiet minute before its low-load
matrix; this run used the stricter five-minute check for every trial.

Sampling every five seconds retained host CPU, pressure, disk counters and build
process presence. A known idle Cargo/test pair was allowed only with unchanged
CPU/I/O counters and process start identity. Immediate post-trial observations
are also retained. `host-validation.json` records preflight and trial intervals,
sample counts and maximum sample gaps. Monitoring includes the initial wait,
not just accepted intervals. The monitor writes snapshots atomically; its exact
source is archived separately from the unchanged benchmark harness.

No unrelated processes were stopped. Desktop applications and the user's stack
remained active. This is not dedicated-host qualification: sampling can miss
very short jobs, and other applications can still share CPU or storage. The
same small-data and 45-second screening limits as the original report apply.

## Archive contents and verification

- `matrices/*/`: full individual manifests, compressed trial/cleanup reports,
  validated CSVs and per-matrix summaries.
- `followup.json`: predeclared order/rule, outer repeats, timing, all attempts,
  overlap decisions and immediate post-trial observations.
- `all-attempts.csv`, `trials.csv`, `summary.json`: every attempt and the quiet
  comparison. Both CSVs contain the same 12 rows because no attempt was excluded.
  Summaries use medians/ranges of trial statistics, not pooled percentiles.
- `host-observations.json.gz`, `host-validation.json`: host evidence and interval
  validation, including preflight waiting.
- `worker-failure-codes.json`: read-only inspection of all five stopped resync
  failures. The timeout proof is in its individual raw report.
- `orchestration-source.json.gz`: exact temporary runner/archive scripts for
  audit; `/tmp` paths record the execution environment, not a supported API.
- `SHA256SUMS`: checksums for every archive file except itself.

From the repository root, regenerate/validate any individual matrix summary with
`python3 e2e/native/performance/summarize.py PATH_TO_MATRIX_ARCHIVE`. Aggregate
rows preserve the outer repeat identity from `followup.json`; all source matrices
remain independently verifiable. Run `sha256sum -c SHA256SUMS` in this directory
to check integrity. See the [harness instructions](../../../../e2e/native/performance/README.md)
for reproducing the workload.
