# SP07 diagnostic controls — results and limits

The six accepted 8-core controls are complete. Two earlier pairs had detected
build overlap and were repeated as whole pairs; all four excluded trials remain
in this archive. All ten measured trials pass correctness, cleanup, input and
five-second p95 at achieved input. Functional smoke pairs remain separately
archived and excluded from these performance denominators.

**Disposition: retain the diagnostic observer with explicit costs and limits;
defer a source runtime patch.** The severe SP06 input shortfall did not recur.
Its cause remains unresolved in [#127](https://github.com/supabricks/platform/issues/127).
This is not a source fix, performance improvement, or blanket capacity guarantee.
The original 737/839-row/s pair remains in the SP06 denominator.

| Accepted trials | Input range (changed rows/s) | Publication p95 range (ms) |
| --- | --- | --- |
| Six: three sampler-off/on pairs | 1,245.849–1,248.158 | 3,704.101–3,757.006 |

Both arms retain eight clients, identical runtime/workload/worker profiling,
1,250 offered rows/s, 60-second warmup and 300-second measurement. The frozen
controller is `5c79acf`; the frozen workload harness is `be4701c`; the accepted
runtime revision is `e10d515`. No durability, replication semantics or transaction
shape changed. The [predeclared protocol](../../sync-performance-sp07.md) and
[initial attribution](../2026-09-30-sp07-attribution/README.md) remain authoritative.

## Observer cost

Paired median changes to the fixture are −0.083% input, −0.058% publication p95,
+0.219% CPU and +0.010% peak memory. Individual pairs move in both directions;
none changes an input/freshness outcome or exceeds the 10% resource/latency trigger.
Three repetitions do not establish zero overhead or statistical certainty.

Separately, the host controller adds **0.0384 average CPU cores** with the sampler
active. This is outside the fixture's 16-GiB cgroup. Accepted-trial sampled
controller RSS is 212–233 MiB and its lifetime peak reaches about 330 MiB.
The same controller persists across arms; allocator retention and export work
confound the negative paired RSS delta. Do not claim a memory improvement or
an isolated incremental memory cost. Raw sample budgets and all resource reports
are retained, including excluded attempts.

## Accounting correction and visibility

The first automated analysis summed process I/O counters by cgroup and called the
result a lower bound. That interpretation was wrong: Linux documents
[`/proc/pid/io`](https://man7.org/linux/man-pages/man5/proc_pid_io.5.html) as including
waited-for children. Summing observed parent and child counters can count work
more than once; process counters also cannot simply be substituted for device or
cgroup accounting. The resulting grouped byte totals are **invalid**.

`superseded-grouped-io-analysis.json` and `superseded-analysis-receipt.json` retain
that original output solely for audit. Do not use their grouped I/O figures.
The correction is tracked in [#130](https://github.com/supabricks/platform/issues/130).
`observer-review.json` retains each stable process identity separately, explicitly
labels inclusive semantics, and adds independently recorded fixture `io.stat`
deltas over the load bookends. It does not produce additive host I/O totals.
A parent/child regression test prevents the original interpretation returning.

Permission denials and process turnover remain explicit in every sampler-on trial.
Many root-owned processes are unreadable, and short-lived processes may escape
observation. Consequently, the diagnostics neither isolate the earlier cause nor
prove that no unrelated I/O occurred. The offline correction changes no raw
sample, latency/input/resource result, trial acceptance or retry decision; no
benchmark rerun is needed to correct this interpretation.

## Verification and next decision

All **71 local accounting tests pass**. CI passed on diagnostic head `e883fa8`;
final-head checks remain required after this evidence/accounting correction.

Reproduce the corrected report, verifying every recorded artifact hash:

```bash
build/sp06-controller-venv/bin/python e2e/native/performance/host_io_analysis.py \
  docs/architecture/sync-performance-evidence/2026-10-01-sp07-controls/controls \
  --output /tmp/sp07-observer-review.json
```

`disposition.json` binds that report and analyzer versions. `SHA256SUMS` binds the
whole evidence tree, including the child archive's manifest. `controls/` preserves
all accepted and excluded trial/profile/cleanup records and host samples.

No source change is demonstrated necessary by the source-qualified input and
these successful controls. A future recurrence with better attribution can justify
a separate SP07 source intervention, with source-only/full-stack comparisons and
crash/recovery testing. Keep #127 open. Controller disk/failure-state follow-up
remains #126; sustained maintenance and release qualification remain SP11/SP12.
