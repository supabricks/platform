# RocksDB retired-file accounting correction (#154)

The original accounting implementation rejected an owned regular SST file with zero links during native compaction. Increased accounting frequency reproduced the failure on its first resource-cycle fixture: 000245.sst, UID 1000, mode 0100644, link count 0, observed size 542,778 bytes; the next path lookup returned FileNotFoundError. Original failures and rejected metadata are retained. Ordinary diagnostic repeats were stopped after this independent reproduction established the mechanism; their interrupted output is retained, not counted as passing qualification.

The fix accepts zero or one link for an owned regular file and counts its observed bytes for the current sample. Multiple hardlinks, symlinks, nonregular entries, foreign ownership and permission errors remain fatal. No content is read, no write is retried, and the observation grants no replay or feedback authority. This does not establish a hard disk quota.

Validation of source dcca9cfc80c4ef87d92a6eafa32be18a86b005dd:

- Seven deterministic accounting cases: the original code fails the retired-file accounting/budget cases; the corrected full native suite passes all 42 tests.
- Twenty repeated native resource-cycle fixtures pass, with 455,000 scans and 46 natural retired-file observations.
- Linux CI: 42 contract tests and five stress fixtures pass, 113,750 scans and 17 natural retirements.
- macOS CI: 42 contract tests and five stress fixtures pass, 113,750 scans and four natural retirements.
- [CI run](https://github.com/supabricks/platform/actions/runs/37133322207) retains the platform artifacts. Matrix fail-fast is disabled so both platforms produce independent outcomes.
- Three alternating before/after component pairs over 32 regular files, 3,000 scans per arm: median 104.323 us before and 104.412 us after (+0.086%). This is a hot-filesystem accounting component measurement, not a pipeline speedup, source-capacity result, or statistical equivalence claim.

Native stress changes accounting frequency to expose the race; it is correctness qualification, not a workload for performance comparison. The original 108-fixture SQLite owner-correction campaign remains unchanged and complete. The new experimental RocksDB package uses the already-qualified owner correction; old packages and failed sequences remain preserved.

Installed package `rocks-runtime-04`, release `dbc0a4127d79b3009776df07556c189fa686529bf1c7ea84a5d78de9f7c8dbe6`, passes strict verification and all four screens: bounded reads, full lifecycle, observer off and observer on. All cleanup receipts show zero leaked or remaining descendants. The short screens establish readiness/correctness, not sustained source capacity or an engine speedup.
