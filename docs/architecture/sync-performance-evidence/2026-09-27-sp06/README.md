# SP06 source qualification and comparison evidence

Status: **source input qualified; higher-load full-stack comparison pending**.
Reviewed 2026-09-27 (America/Chicago). No runtime change or end-to-end throughput
improvement is claimed. [Protocol](../../sync-performance-sp06.md).

## Completed measurements

| Phase | Accepted trials | Other outcomes |
| --- | ---: | --- |
| Source screen | 18 | One contended three-trial block retained and excluded |
| Source profiler off/on controls | 12 | All six pairs uncontended |
| Four-logical-CPU envelope | 6 | All three blocks uncontended |
| Historical four-client comparison | 24 | All 12 pairs correct/fresh at achieved input |
| Historical full-stack profiler controls | 18 | All nine pairs correct/fresh at achieved input |

Total: 78 accepted trials. All published attempts and teardown receipts are
retained in the phase archives. The earlier recovered controller dependency
failure and original temporary-evidence loss remain separately documented in
[the recovery record](../2026-09-27-sp06-recovery/README.md). None is counted here.

## Source capacity

Median achieved changed rows/s across three 300-second repetitions:

| Logical CPUs | 4 clients | 8 clients | 16 clients |
| --- | ---: | ---: | ---: |
| 4 (separate envelope) | 838.13 | 1,666.43 | Not tested |
| 8 | 827.47 | 1,704.47 | 2,640.81 |
| 16 | 831.85 | 1,727.95 | 2,705.46 |

Eight is the smallest tested client count meeting the predeclared 1,250 changed
rows/s floor in every complete minute, at both gating CPU sizes and with profiling
on and off. The worst required minute is 1,671.23 rows/s, 33.70% above that floor.
Four clients do not meet even the 1,000-row/s floor. Increasing client concurrency
supplies the required input without changing transaction shape or durability.
The four-CPU envelope is informative but does not replace the 8/16-CPU gate.

For eight clients, active-client wait samples are approximately 52% SyncRep,
37% WALWrite lock and 10% WalSync at both CPU sizes. Sampled waits are not exact
time attribution. Safekeeper mean flush observations are 2.82–2.87 ms, with the
p95 in the <=10 ms histogram bucket. These observations are consistent with
commit-path waiting and explain why more source concurrency helps; they do not
isolate one storage component as the sole cause. No source runtime fix is justified
by the current target-input shortfall alone. SP07 remains conditional on later
findings, not a prerequisite created by the old four-client generator limit.

## Observer review and decision

The automatic continuation conservatively stopped the higher-load phase because
all source profiler pairs had a throughput loss. `automatic-review-hold.json`
preserves that decision. The protocol requires investigating smaller repeatable
losses; it does not require zero diagnostic overhead.

The paired source controls measure median throughput costs of 0.558% at 8 CPUs
and 0.499% at 16 CPUs (individual losses 0.233–1.205%). Median CPU costs are 5.16%
and 5.46%; transaction-p95 costs are 0.72% and 0.57%; peak-memory costs are 2.94%
and 2.23%. Both profiling states retain the required capacity in every minute,
with no threshold crossing, source mismatch or teardown failure. CPU cost is
roughly 0.03–0.04 additional average cores, not several extra cores.

Historical full-stack activation controls also show small overload input costs
(median 0.625%/1.466% at 8/16 CPUs), while all runs remain correct and below the
five-second p95 gate at the input actually supplied. Their offered 1,000-row/s
probes still achieve only about 725–742 rows/s with four clients. They do not
establish 1,000-row/s replication. The unchanged-runtime old/new-harness comparison
has no correctness/freshness failure and no consistent throughput improvement.

**Decision: qualify the eight-client source profile and proceed.** Retain and
report the measured observer cost; do not subtract it from results or alter the
profiler. The paired intervention identifies activation overhead, but individual
probe costs are not separately isolated. Three repeats do not establish statistical
certainty. Use the same diagnostic activation for both higher-load comparison
arms, then perform the separately required off/on controls at that load.
`source-qualification.json` records the decision, inputs, limitations and the
frozen `sp06-source-qualified-8clients-v1` profile: eight clients, 1,250 offered
changed rows/s, 60-second warmup and 300-second full-stack load at 8/16 logical
CPUs. Six comparison pairs plus six control pairs remain (24 trials).

## Reproduction and retention

Each child `SHA256SUMS` binds the exported experiment, all structured trial
receipts/profiles, host observations and frozen source code. The root manifest
also binds summaries, review code and the decision. Raw numeric acknowledgment
samples remain available; private logs, database contents and credentials are
excluded. Keep every rejected attempt visible; do not pool contended outcomes
with the accepted screen.

`review_source.py --campaign PERSISTENT_WORKSPACE --repo REPOSITORY --output FILE`
verifies archived hashes, recomputes all three source phase summaries, checks
capacity in both control arms and reviews the historical comparisons. It records
bounds describing this particular review, not new generally relaxed acceptance
thresholds. `source_analysis.py` is the exact offline analyzer used here. The
frozen measurement harness remains `be4701c`, runtime `e10d515`, package manifest
`87e08c546e1316759457443b51f048f0cc7774e109b09d7176c7bf1e65cf7d40`.
