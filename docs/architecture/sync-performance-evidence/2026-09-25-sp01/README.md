# SP01 journal contention recovery evidence

The primary experiment contains twelve accepted predecessor/candidate pairs
(24 trials) and two contended pairs retained and replaced in full (four trials).
Six activation controls and six follow-up trials bring the final-runtime archive
to 40 trials, all with clean teardown receipts. Failures remain outcomes. A separate six-trial, 4-CPU low-load follow-up is declared
in [followup-declaration.json](followup-declaration.json); it does not replace any
original trial. See the [human report](../../sp01-journal-contention-recovery.md)
and [decision](decision.json) for interpretation.

- `experiment.json`: immutable package/harness identities, seeded order, every
  attempt, environmental validity, original and sanitized evidence hashes.
- `comparison.json` and `comparison.md`: all four cells, individual paired deltas,
  failure denominators, medians/ranges and original historical baseline deltas.
- `journal-analysis.json`: all accepted trials' bounded operational receipts and
  journal spans, derived by `journal_analysis.py`. Counters include the **whole
  fixture** (bootstrap/warmup/load/drain); they are not steady-state rates. Missing
  predecessor counters are null, never zero. Incomplete worker tails are retained.
- `progress-analysis.json`: load-window resource counters, source shortfalls and
  post-stop uncaptured transaction lower bounds; reproduced by `progress_analysis.py`.
- `activation-controls/`: three same-package profiler off/on pairs on final b6a0b4b.
- `low-load-followup/`: the separate three-pair variability check.
- `superseded/`: earlier completed calibration blocks in `completed-controls.tar.gz`,
  including a contended pair;
  interrupted/pre-trial manifests and host records. These are never mixed into the
  final runtime comparison. The interrupted comparison-01 predecessor trial has
  no complete cleanup receipt and no accepted performance outcome; its owned
  container was removed and absence verified in `superseded.json`.
- `packages/`: exact installed inventories and overlay proofs for every runtime
  variant. Decompressed inventory SHA-256 is its recorded release identity.
- `ci/`: retained macOS freshness failures and reruns; these are different hosts and
  workloads from the local paired matrix, not additional matched trials.
- `host/*.jsonl.gz`: five-second host records including quiet waits and overlaps.
- Each arm directory: matrix, trial, cleanup and compressed worker profile.
- `analysis-source.json.gz`: analysis/export source used for this archive.
- `SHA256SUMS`: hashes of all retained files except the root checksum itself.

Export replaces private installation, checkout and output paths with stable labels.
Raw service logs, scratch, credentials, row values and SQL text are excluded.
The original failed/contended outcomes remain visible. A runtime failure does not
have a complete end-to-end latency, and successful component samples from a failed
pipeline do not establish throughput qualification.

## Reanalyze without running the stack

From the repository root:

```sh
python3 e2e/native/performance/report_comparison.py docs/architecture/sync-performance-evidence/2026-09-25-sp01
python3 docs/architecture/sync-performance-evidence/2026-09-25-sp01/journal_analysis.py docs/architecture/sync-performance-evidence/2026-09-25-sp01
python3 docs/architecture/sync-performance-evidence/2026-09-25-sp01/progress_analysis.py docs/architecture/sync-performance-evidence/2026-09-25-sp01
python3 docs/architecture/sync-performance-evidence/2026-09-25-sp01/combined_low_load.py docs/architecture/sync-performance-evidence/2026-09-25-sp01
```

Run the same commands on `activation-controls` or `low-load-followup`. To
reanalyze completed superseded controls, first unpack
`superseded/completed-controls.tar.gz` into a temporary directory; it contains
`controls-9098239/` and `controls-6ce4439/`, including their original structured
files, profiles, sources and per-directory `SHA256SUMS`. Run the report and journal
analysis commands against those extracted directories. Archive packing was checked
against every original file hash before removing the duplicate expanded copies. The incomplete superseded experiments are retained
for provenance only and intentionally cannot pass the completed-report validator.
From this directory, `sha256sum -c SHA256SUMS` verifies the final archive.

## Diagnostic package reproduction

Both arms inherit the same SP00 diagnostic base, including unchanged native and
Python probes. This is a measured diagnostic overlay, not a signed distributable
release; source revisions are operator assertions. Use the original base package
whose manifest SHA-256 is `5e06525d48333da3a5b4c0587eb2a0a085d626dea3a1eca99cee28dfc152286e`.
Check out the final source revision `b6a0b4b546de460308f6adccfe6aec34528d1893`, then:

```sh
cargo build --locked --release -p supabricks-local --features sync-profile --bin supabricks
python3 /path/to/this/evidence/overlay.py --repo "$PWD" \
  --base /path/to/unchanged-sp00-package --destination /path/to/new-sp01-package \
  --proof /path/to/overlay-proof.json
```

The parameterized recipe copies the installed tree, breaks hardlinks before each
change, updates only the binary/two Python modules/their checked-hash bytecode and
inventory, and verifies the base stayed unchanged. Compare the resulting proof
with `packages/b6a0b4b/overlay-proof.json`; verify the complete installed inventory
before qualification. A toolchain/build change may change bytes and constitutes a
new package identity requiring new measurement. Original recipe hashes accompany
the path-parameterized published copy.

Run `compare.py` from the clean shared harness revision
`c52f466ffc47e141d6decf632f455c2cc4ecf6f2`, using that checkout for both harness arms,
the original base as predecessor revision `92272759ad06af6207b9537a02871f0477e17295`,
and final SP01 package/revision above. Preserve all recorded parameters, qualifier
image ID, five-minute rolling quiet policy and affinity. Default cells/repeats
produce twelve pairs. For activation controls use the same SP01 package/revision
on both arms with `--activation-control --cells 4:50`. For the separate follow-up,
use predecessor/candidate normally with `--cells 4:50 --seed 20260925`.
