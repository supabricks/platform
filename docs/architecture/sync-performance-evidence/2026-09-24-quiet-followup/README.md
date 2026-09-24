# Matched synchronization follow-up — 2026-09-24 UTC

[Main report](../../sync-core-scaling.md) · [Original evidence](../2026-09-24-local/README.md)

Three new repeats of each configuration affected by the original late external
build: 4/16 logical CPUs at 50 changed rows/s, and 8/16 CPUs at 1,000 offered
rows/s. This archive supplements the original 27 trials; it does not replace,
filter, or pool their statistics. Repeats were selected by configuration before
their outcomes were known.

## Outcomes

| CPUs | Offered rows/s | Complete / trials | Trial p95 median (min–max), seconds |
| --- | --- | --- | --- |
| 4 | 50 | 3 / 3 | 3.696 (3.677–3.698) |
| 16 | 50 | 3 / 3 | 4.097 (3.660–4.438) |
| 8 | 1,000 | 0 / 3 | Unavailable: all require resync |
| 16 | 1,000 | 0 / 3 | Unavailable: all require resync |

All six 50-row/s trials met input rate and the five-second p95 gate, passed full
two-table equality, and had clean teardown. The overload runs had four warmup
failures (all three on 16 CPUs and one on 8 CPUs) and two failures during drain
after measurement (8 CPUs). Their achieved source rates were 654.211 and 683.303
rows/s; neither is a successful replication throughput measurement. Every failed
fixture recorded `incremental_worker_failed`. All 12 completed attempts passed
owned-process cleanup with zero leaked or remaining descendants.

## Execution and isolation

The low-load matrix ran 04:22:44–04:31:17 UTC, after 60 seconds without observed
active external builds. It has 102 host observations during the full matrix,
with no active build samples. An initial overload trial then overlapped new
compiler jobs and timed out after accepting 636.964 rows/s. The next trial was
interrupted when this overlap was identified; its owned container was removed.
Both attempts are retained separately, without a fabricated completed matrix or
latency result for the interrupted trial.

The subsequent overload series waited for five quiet minutes before each trial.
Its predeclared rule was to retain every attempt and repeat any attempt overlapped
by active build samples, irrespective of success, latency, or failure. The six
trials ran 04:53:34–05:01:12 UTC with no overlapping build samples and no retries.
Order was randomized with seed `20260923`, separately within each load group.
The low-load group used the matrix's three repeats. Each high-load trial used a
one-trial matrix, with its outer repeat identity recorded in `followup.json` and
the aggregate CSV; each individual manifest therefore calls its internal repeat
`1`. No aggregate matrix manifest is fabricated.

Host sampling every five seconds retained CPU, pressure, disk counters, and Rust
build process presence. One preexisting idle Cargo process and its test child
were allowed only with unchanged CPU/I/O counters; this was verified in every
accepted interval. The archive includes preflight waiting and the renewed build
activity, not just quiet windows. Desktop applications and the user's stack
remained active. “Quiet” does not mean a dedicated host, zero I/O wait, or proof
that no sub-five-second job ran. No unrelated work was killed or suspended.

## Provenance and unchanged settings

- Runtime revision: `a8fd376536d3d6c5198df0badb6ee13cfaa6702f`.
- Binary SHA-256: `a521c26a3c12c559e3b2cdce8cc946b631378772f52cd61bec332ddeb85abff9`.
- Package manifest SHA-256: `78928698010df68ad72717b042728148abcb48a01774890ff3efdd6af3d9bb48`.
- Qualification image: `sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec`.
- Harness revision: `f3dacfe4ecf3192eb84e28a2503337c7a5467800`, clean throughout execution.
- Same CPU affinities, 16 GiB memory, disabled container swap, four clients, two
  10,000-row tables, 45-second measurement, 120-second drain, default sync policy,
  fresh per-trial fixtures, runtime inventory verification, and full-table checks
  as the original matrix. No runtime patch or exception instrumentation was used.

The harness added a post-stop capture-evidence guard since the original matrix.
It changes classification of ambiguous drain timeouts, not load generation or
runtime behavior. None of the 12 uncontended repeats used that timeout path.
Exact per-file harness hashes and resource limits are in each manifest.

## Files and reproduction

- `rate50/`: full six-trial manifest, compressed reports, validated CSV and summary.
- `rate1000/`: one manifest/report bundle per attempt, outer `followup.json`,
  `all-attempts.csv`, quiet-comparison `trials.csv`, and grouped summary. The two
  aggregate CSVs have identical rows because no new attempt needed exclusion.
- `contended-overload-attempt.json.gz`: the earlier incomplete matrix, one complete
  contended trial and available interrupted-attempt records. Excluded from quiet
  summaries; never represented as a complete two-trial experiment.
- `*-host-observations.json.gz` and `host-validation.json`: observations and
  interval checks, including unchanged counters for the known idle processes.
- `worker-failure-codes.json`: read-only inspection of all six stopped failures.
- `orchestration-source.json.gz`: exact temporary orchestration scripts retained
  for audit; their `/tmp` paths are execution provenance, not a supported API.
- `comparison.png`, `comparison.svg`, `plot-comparison.py`: standalone comparison
  plot and its source (Matplotlib 3.11.2). Failed trials never supply lag samples.
- `SHA256SUMS`: all archive files except the checksum file itself.

From the repository root, validate/regenerate any individual matrix summary with
`python3 e2e/native/performance/summarize.py PATH_TO_MATRIX_ARCHIVE`. The root
`rate1000/trials.csv` concatenates its six matrix CSV rows, preserving outer repeat
identity and linking each source manifest. Its summary contains medians and
ranges of available trial statistics, with sample counts; never pooled p95s.
`python3 docs/architecture/sync-performance-evidence/2026-09-24-quiet-followup/plot-comparison.py`
regenerates the comparison when Matplotlib is installed. See the
[harness instructions](../../../../e2e/native/performance/README.md) to rerun the
same workload; independently verify host activity throughout every new run.
