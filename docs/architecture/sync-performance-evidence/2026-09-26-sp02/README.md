# SP02 evidence

Runtime and shared harness are frozen at `838f0b1d4977489526bbd6e77d036569f3df83a9`. The predecessor runtime is merged SP01 `485b552ea5c802d4e446433b300f95851ce5aa6c`, with the same updated profiling code installed in both arms. These are inventoried diagnostic overlays of the SP01 package, not signed release qualification.

- `component-screen/`: 33 final randomized durable component trials, selection declaration/result, host receipts and independent stopped-file correctness checks.
- `predecessor-controls/`, `candidate-controls/`: three profiling-off/on pairs each at 4 CPUs and 50 changed rows/s. Both arms within each control use the same package.
- `main/`: all 24 mandatory paired trials, including six predecessor drain timeouts; no contention replacements.
- `profile-analysis.json`: additional main-matrix group/cursor/stage/resource summaries.
- `overload-controls/`: twelve SP02 profiling-off/on overload controls, all passing.
- `decision.json`: measured contribution and explicit qualification limits.
- `packages/`: exact diagnostic inventories and overlay proofs, including before/after hashes and relative checked-hash bytecode.
- `validation.json`: local fault/accounting test results; `ci/`: GitHub qualification records and retained macOS burst failure.
- `superseded/`: the original deadline-race candidate's component/control receipts and incomplete overload attempt, retained separately from final acceptance.

Decision: **Keep — performance**. The [human report](../../sp02-durable-capture-groups.md) records the contribution, resource cost and remaining limits. Final evidence retains 48 full-stack trials plus 33 component trials. Six predecessor drain timeouts remain failures; all final SP02 mandatory trials and all activation controls pass. No contention replacement or cleanup failure occurred.

Recompute component results with `python3 component_analysis.py component-screen`. After an experiment completes, `python3 profile_analysis.py EXPERIMENT_DIRECTORY` derives all-run capture statistics, sampled feedback bounds and per-stage trial p95 ranges. The frozen runner's `analysis-source.json.gz` in each exported experiment contains all Python analysis modules; its `comparison.py` reports sample counts, ranges and paired deltas without treating failed trials as latency samples.

Each exported experiment retains original artifact hashes and sanitized export hashes. Private fixture paths, logs, SQL and source data are excluded. Native profile records contain fixed labels and numeric counters. Sampling and process-death tests do not qualify power loss. Full-stack results do not infer 1,000-row/s capacity when the four-client source delivers less than that input rate.
