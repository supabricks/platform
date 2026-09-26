# SP03b evidence

**Decision: keep for performance, retaining SP02 grouping.** The frozen runtime
and common harness are 5382e80032bb4fc70706f112db26f8415849a961. The predecessor
runtime is SP03a da548e7, with the identical updated capture profiler.

[Report, protocol and limitations](../../sync-performance-sp03b.md) explain the
measured 8–9% achieved-throughput gain, roughly 44% lower capture native sync calls
per transaction, 7–9% total CPU cost, and physical-capacity tradeoff. Sustained
1,000 rows/s remains unqualified. SP04 is next; [#116](https://github.com/supabricks/platform/issues/116)
tracks retained WAL allocation for a later SP05 experiment.

All 108 declared measurements are retained: 72 full-stack trials and 36 component/
control trials, with no detected build/test overlap. All 66 grouped full-stack
trials are correct/fresh. Three ungrouped DELETE trials time out; three ungrouped
WAL trials complete correctly but miss freshness. All 36 component/control trials
pass independent correctness. Thirty superseded component trials and two earlier
admission attempts with zero trials remain separate from acceptance.

- component-screen/: 30 final-candidate trials, including six native profiling controls.
- reader-controls/: six separately predeclared read-observer controls, native profiling off.
- predecessor-controls/ and candidate-controls/: 18 full-stack off/on trials each.
- main/: 24 fresh paired trials; all correct/fresh.
- delete-ablation/ and wal-ablation/: six full-stack grouping interaction trials each.
- packages/: immutable diagnostic package proofs, compressed inventories and logical size deltas.
- validation/: local and installed diagnostic checks.
- ci/: signed installed qualification, source-tree/worker proofs, frozen-runtime
  checks, original catalog/environment/project failures and bounded retry outcomes.
- superseded/: the earlier 1debe4f component screen (all 30 trials correct and
  uncontended), package proofs and validation. Excluded after the migration
  admission correction in #113. No full-stack trials ran on earlier candidates.

Diagnostic packages are unsigned immutable overlays; signed Linux/macOS installed
qualification is recorded separately. Both installed WAL/sync gates pass. The
frozen native-release workflow passed after one retry of #114/#115; the separate
Linux catalog probe failed twice (#110). Every failure remains retained. Process
crash tests do not establish physical power-loss behavior.

The generic comparison retains COMMIT-only metrics and a legacy checkpoint
capability label. wal_analysis.py and wal_paired_analysis.py include explicit
checkpoint work and all capture native sync costs. Profile-disabled counters are
unavailable, never zero-cost evidence. profile_analysis.py separates full worker
lifetimes from sampled load windows. resource_analysis.py records partial process
CPU coverage without equating it to cgroup totals. Three repeats are a screening
experiment, not a statistical certainty claim.

Recompute from this directory (Python standard library only):

```sh
python3 -B component_analysis.py component-screen
python3 -B component_analysis.py superseded/component-1debe
python3 -B reader_analysis.py
python3 -B analyze_slice.py
python3 -B wal_analysis.py
python3 -B wal_paired_analysis.py
python3 -B profile_analysis.py main
python3 -B profile_analysis.py delete-ablation
python3 -B profile_analysis.py wal-ablation
python3 -B resource_analysis.py main
sha256sum -c SHA256SUMS
```

These correspond to component/reader summary.json, paired-summary.json,
wal-summary.json, wal-paired-summary.json, and the named profile/resource outputs.
Per-experiment WAL summaries and main-wal-paired-summary.json are exact subsets
of those full outputs. The frozen comparison source is retained in each
analysis-source.json.gz; raw trial/profile/host/cleanup records and every attempt
are included. Component drivers are additionally retained under protocol-drivers/;
the reader-control archive contains its driver. Original package paths in those
drivers identify the immutable local overlays; rebuild them using the frozen
wal_package.py and packages/ proofs before a new run.

SHA256SUMS binds all files below this directory except itself and interpreter
caches, including child checksum manifests. Child manifests bind their own
archives. Generated summaries contain no private source values or SQL traces.
