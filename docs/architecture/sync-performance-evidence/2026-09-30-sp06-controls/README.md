# SP06 final controls and measurement disposition

All required SP06 measurements and their review are complete: **102 accepted
trials**, with all recoverable excluded/failed attempts retained. This completes
the measurement-enabling slice; it does not establish a blanket throughput or
release guarantee. The runtime, durability settings and frozen harness are
unchanged. PR #121 remains subject to required checks and merge review.

The [source qualification](../2026-09-27-sp06/README.md) selects eight writers.
The [higher-load main checkpoint](../2026-09-30-sp06-full-stack/README.md) retains
both the earlier 8-core input misses and the interrupted disk-full control
campaign. This archive adds the fresh, separately named `qualified-controls-02`
campaign: six profiler off/on pairs, 12 trials, no replacements or detected build
overlap. Both arms use the same accepted package, eight clients, 1,250 offered
changed rows/s, 60-second warmup and 300-second measurement at 8/16 logical CPUs.

| Logical CPUs | Passed trials | Actual input range (rows/s) | Publication p95 range (ms) |
| --- | --- | --- | --- |
| 8 | 6/6 | 1,242.011–1,246.707 | 3,710.785–3,734.203 |
| 16 | 6/6 | 1,245.437–1,248.978 | 3,712.429–3,771.130 |

Every trial passes exact final-table correctness, cleanup, input admission and
five-second p95 at actual supplied input. The previous failed control attempt
remains excluded with no final cleanup receipt; its later recovery does not
retroactively qualify that attempt.

## Observer review

Paired median percentage changes, profiling on relative to off:

| Logical CPUs | Input | Publication p95 | CPU | Whole-fixture memory peak |
| --- | --- | --- | --- | --- |
| 8 | −0.294% | −0.103% | +6.269% | +6.574% |
| 16 | −0.147% | +0.583% | +5.149% | +5.348% |

The repeatable input cost remains visible; neither CPU group crosses the input
or freshness threshold. CPU cost is about 0.08 average cores, and median resource
costs stay below the 10% investigation trigger. We retain and disclose overhead
instead of subtracting it from the results. The activation experiment measures
the combined profiler cost, not isolated individual probe costs.

The first 16-core profiler-off trial peaks at 4.71 GB, versus about 1.9–2.0 GB
in the other off trials. Its pre-load cgroup memory is already 4.10 GB; the peak
includes setup and warmup. The outlier is retained. Its cause is not isolated,
and the negative paired memory delta is not evidence that profiling saves memory.
No memory capacity claim or outlier removal follows from this observation.

## Disposition and remaining work

Source input capacity is independently established with eight clients. In the
main matched comparison, all six 16-core trials also supply about 1,249 rows/s
at sub-five-second p95. The fresh controls reproduce target supply on both CPU
sizes. These are bounded local five-minute observations.

The earlier accepted 8-core main pair remains at 839/737 rows/s. Higher source
and capture COMMIT times identify the immediate limit, but its root cause remains
unresolved. Fresh successful controls are not replacements for that pair.
[Issue #127](https://github.com/supabricks/platform/issues/127) requires further
storage/commit attribution before choosing any source-code intervention. The
concurrency screen does not justify a speculative SP07 runtime change, and the
full record does not justify a universal 8-core 1,000-row/s claim.

The controller disk-admission/failure-recording follow-up remains in
[#126](https://github.com/supabricks/platform/issues/126). The source recovery
limitations in #124 remain explicit. SP11 sustained maintenance/recovery and SP12
release qualification are still required. Local CPU affinity results are not
EC2 instance-scaling evidence.

The accounting check at checkpoint `44c3d29` timed out during checkout before
running tests. The final change gives checkout/setup a 15-minute whole-job budget
while retaining a separate five-minute accounting-test timeout. Tracked in [#128](https://github.com/supabricks/platform/issues/128). All 60 local
accounting tests pass. Latest CI status must be checked on the final PR head; the canceled run is not a test pass.

## Evidence and reproduction

`controls/` contains every public structured report, compressed profile, host
observation, immutable identity and checksum from the completed campaign.
`final-review.json` retains all per-trial outcomes and paired overhead deltas.
`SHA256SUMS` also binds the child checksum list and this review source.

```bash
build/sp06-controller-venv/bin/python \
  docs/architecture/sync-performance-evidence/2026-09-30-sp06-controls/review_controls.py \
  --repo . --output /tmp/sp06-final-review.json
```

This validates the archive hashes, recomputes the trial/profile/cleanup outcomes,
checks profiler activation and host overlap, and regenerates `final-review.json`.
