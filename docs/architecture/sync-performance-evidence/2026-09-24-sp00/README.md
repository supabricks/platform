# SP00 unchanged-runtime comparison evidence

The mandatory experiment contains **12 predecessor + 12 candidate trials**, all
accepted without environmental replacement. Runtime failures remain outcomes.
The separate [low-load follow-up](low-load-followup/comparison.json) contains six
additional completed trials under the predeclared [decision](followup-declaration.json).
All six pass, but no original result is replaced. The [combined analysis](low-load-combined.json)
retains the original freshness miss; [combined_low_load.py](combined_low_load.py)
reproduces it. See the [human report](../../sp00-reproducible-comparisons.md) and
[contribution decision](decision.json).

- [Experiment and immutable identities](experiment.json)
- [Machine-readable comparisons](comparison.json), [compact table](comparison.md)
- [Journal-read inspection](journal-read-analysis.json), reproducible with [journal_reads.py](journal_reads.py)
- [Instrumentation consistency](instrumentation-consistency.json)
- [Diagnostic package construction hashes](package-instrumentation.json)
- `runtime-inventory.json.gz`: exact installed manifest; its decompressed SHA-256
  is the recorded release identity.
- `analysis-source.json.gz`: repository analysis/export source at archive time.
- `host/*.jsonl.gz`: bounded five-second host samples, including the quiet wait.
- Each arm directory contains its matrix, trial, cleanup and compressed profile.
- `SHA256SUMS`: hashes of every retained artifact except this checksum file.

Local harness, installation and output paths are replaced with stable labels.
Original artifact hashes are retained alongside export hashes in the experiment.
Private service logs, scratch, credentials, source values and SQL text are excluded.
Package, runtime and frozen harness identities are unchanged by export.

From this directory, `sha256sum -c SHA256SUMS` verifies the archive. From the
repository root, re-run analysis without launching a runtime:

```sh
python3 e2e/native/performance/report_comparison.py docs/architecture/sync-performance-evidence/2026-09-24-sp00
python3 docs/architecture/sync-performance-evidence/2026-09-24-sp00/journal_reads.py docs/architecture/sync-performance-evidence/2026-09-24-sp00
```

The report separates correct completion, freshness and input-rate success. An
incomplete trial has no complete latency. Historical deltas are not controlled
causal effects; available component metrics in failed pipelines describe their
stated sampled windows and successful worker subset.
