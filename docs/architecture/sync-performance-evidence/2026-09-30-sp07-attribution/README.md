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

The first functional smoke pair is retained in `initial-functional-smoke/` and
is excluded from performance denominators. `initial-observer-controls/` preserves
the subsequent admission hold with **zero measured trials**: controller CPU/RSS
accounting was added before full-duration measurement. The original waiting
manifest was not reset or reclassified. `admission-hold.json` records the reason.
The subsequent campaign uses a new frozen controller and distinct output names.

The revised functional pair (`functional-smoke-02/`) also passed. Both arms
retain controller CPU/RSS samples; the candidate additionally retains a bound
container cgroup and host process I/O. `functional-review.json` verifies that
analysis works, but its short-run values are **not** performance results.
The six full-duration trials are running or waiting under `observer-controls-02`
with frozen controller `5c79acf` and unchanged workload harness `be4701c`.

`run_campaign_02.py` and `review_when_complete.py` are exact launch/automatic
analysis recipes, originally located in `build/sp07-20260930/` (not executable
from this archive). The latter recomputes results after all trials are archived;
it does not automatically assert causality or select a source patch.
