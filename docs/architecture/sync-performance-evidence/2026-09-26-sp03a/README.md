# SP03a SQLite qualification evidence

Status: complete. Keep for reliability/enabling; no speedup established.

The [report](../../sync-performance-sp03a.md) declares the experiment and records
its interpretation. Candidate source/harness: `da548e7b7564d554018a9b34281e9e42320028ee`.
The predecessor package is accepted SP02 runtime `838f0b1`, subsequently merged
at `a88eb145`. The same frozen candidate harness runs both arms. Capture, apply,
dependency pins, profiler and workload code are unchanged; explicit native
installation inspection and the packaged SQLite policy are the only candidate
package changes. These local diagnostic packages are not signed release archives.

- `component-screen/`: all nine fixed capture component trials, host observations,
  source hashes, summary and independent correctness results.
- `packages/`: exact manifest inventories, candidate overlay proof, and source
  invariants against accepted SP02.
- `sqlite/linux-diagnostic.json`: actual installed diagnostic Rust and Python
  SQLite identities plus the separate host qualification-reader identity.
- `validation.json`: focused test results; release qualification is recorded
  separately from local unit/fault tests.

The signed Linux and macOS CI receipts match the reviewed source IDs.
No WAL-mode, power-loss, sustained-capacity or 1,000-row/s qualification is implied.
An older read-only harness SQLite is explicitly not qualified for concurrent
live-WAL writes/checkpoints. SP03b must qualify every connection's actual usage.

Local paths, private command logs and scratch databases are excluded from the
comparison exports. SHA256SUMS files bind retained structured evidence, including
runtime failures and any rejected contended attempts. Recompute complete paired
exports with the frozen `e2e/native/performance/compare.py` analysis source and
component medians with `python3 component_analysis.py component-screen`.

- `main/`: 24 accepted trials and four rejected contended trials, with raw structured profiles, cleanup receipts, host observations and analysis source.
- `candidate-controls/` and `overload-controls/`: 18 accepted off/on control trials.
- `ci/`: both installed-platform sync receipts and both attempts of the known Linux lifecycle failure (#101); all 47 frozen-head checks passed after the retained retry.
- `summary.json`, `decision.json`: measured contribution and bounded conclusion.

Recompute the slice summary with `python3 analyze_slice.py`. Each paired export
includes its frozen analysis source and checksum manifest. Top-level SHA256SUMS
also covers package and platform evidence. No private command logs are exported.
