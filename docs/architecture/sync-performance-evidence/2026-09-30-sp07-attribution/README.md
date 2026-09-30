# SP07 attribution checkpoint

`retained-analysis.json` recomputes source COMMIT distributions, active PG waits,
safekeeper flush duration/rate and available PG WAL counters from the checksummed
SP06 higher-load main archive and final controls. All profiled attempts are kept,
including excluded pairs. No sampling, runtime or source change is applied to
that historical evidence.

Reproduce with the pinned controller environment:

```bash
build/sp06-controller-venv/bin/python e2e/native/performance/commit_attribution.py \
  docs/architecture/sync-performance-evidence/2026-09-30-sp06-full-stack/qualified-main \
  docs/architecture/sync-performance-evidence/2026-09-30-sp06-controls/controls \
  --output /tmp/sp07-retained-analysis.json
```

The [SP07 protocol](../../sync-performance-sp07.md) defines the separate six-trial
host-sampler control before launch. `source-paths.json` binds the exact inspected
PG/Neon source files to component pins; later sibling-repository revisions are not
used as runtime evidence. New experiment results remain pending.
