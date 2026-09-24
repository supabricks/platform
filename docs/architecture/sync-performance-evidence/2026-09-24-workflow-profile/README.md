# Workflow instrumentation evidence — 2026-09-24

[Workflow profile and findings](../../sync-workflow-profile.md) · [Prior CPU scaling experiments](../../sync-core-scaling.md)

The final matched series (`profile-series-03`) contains six activation controls
and the requested twelve instrumented profiles. Controls and matrix results are
separate. Earlier development attempts, including a halted collector run, remain
under [development](development/README.md); they are not pooled with this series.

## Files

- `trials.csv`: requested twelve profiles, with outer repeat numbers.
- `controls.csv`: three profiling-off/on pairs at 4 CPUs / 50 changed rows/s.
- `all-attempts.csv`: every attempt in the final series, including any replacement.
- `summary.json`: group medians/ranges and complete/failure counts.
- `analysis.json`: load-window capture/daemon deltas, source timings, safe worker
  exceptions, PG wait samples and durable batch/publication joins.
- `details.json`: storage histogram deltas, selected successful apply workers,
  individual sample omissions, and conservative process CPU deltas.
- `findings.json`: cross-trial summaries derived from the preceding two files.
- `matrices/*/matrix.json`: per-attempt runtime/harness/image/host identity.
- `matrices/*/raw-reports.json.gz`: original trial/cleanup records and matrix.
- `matrices/*/profile.json.gz`: fixed-label raw profiles, available only when
  profiling was enabled. Worker cumulative counters are lifetime totals until
  analysis selects a phase. Inclusive spans overlap; incomplete tails are marked.
- `followup.json`: predeclared order, quiet-host rule and every final-series attempt.
- `host-observations.json.gz`, `host-validation.json`: five-second host samples
  and per-attempt five-minute preflight validation. This was a shared desktop,
  not exclusive hardware. Short external jobs can fall between samples.
- `package-instrumentation.json`: baseline and diagnostic manifest/file hashes.
- `validation.json`: source/build identities and focused test results.
- `orchestration-source.json.gz`: exact controller, archive and analysis scripts.
- `workflow-profile.png` / `.svg`: standalone figures generated from these data.
- `SHA256SUMS`: integrity inventory for every archive file except itself.

The runtime is a locally built diagnostic package, not a signed release
qualification. Feature binary SHA-256:
`29d70940c6a6c88c36f3a3c90cdc826d7a5f38ec1797083968b962d296809156`.
Manifest SHA-256:
`5e06525d48333da3a5b4c0587eb2a0a085d626dea3a1eca99cee28dfc152286e`.
The underlying instrumentation runtime source is `92272759ad06af6207b9537a02871f0477e17295`;
the corrected collector is `ca824fc1c14a7de7d036a3f4192e8be7025d1a7b`.
Every matrix pins the collector files independently. The unmodified baseline
installation and its original manifest are retained locally and identified in
the package proof.

Private service logs, daemon configuration, SQL text, credentials and source row
contents are excluded. Safe exception records retain type, SQLite code and
basename/function/line only. An invalid measurement stays invalid even if its
runtime passed table equality.

## Reproduce the analysis

From the repository root, verify the checksum inventory in this directory, then
extract the retained analysis scripts into a temporary directory:

```sh
python3 - <<'PY'
import gzip, json
from pathlib import Path
root = Path('docs/architecture/sync-performance-evidence/2026-09-24-workflow-profile')
out = Path('/tmp/sb-workflow-profile-analysis')
out.mkdir(exist_ok=True)
sources = json.loads(gzip.decompress((root/'orchestration-source.json.gz').read_bytes()))
for name in ('analyze-profile.py', 'profile-details.py', 'profile-findings.py', 'plot-profile.py'):
    (out/name).write_text(sources[name])
PY
python3 /tmp/sb-workflow-profile-analysis/analyze-profile.py docs/architecture/sync-performance-evidence/2026-09-24-workflow-profile
python3 /tmp/sb-workflow-profile-analysis/profile-details.py docs/architecture/sync-performance-evidence/2026-09-24-workflow-profile
python3 /tmp/sb-workflow-profile-analysis/profile-findings.py docs/architecture/sync-performance-evidence/2026-09-24-workflow-profile
```

Those commands regenerate `analysis.json`, `details.json`, and `findings.json`.
The plot script additionally requires Matplotlib. Each archived matrix is also
accepted by `e2e/native/performance/summarize.py`. Original runtime-failed trials
remain in the denominator and never receive a complete latency percentile.
