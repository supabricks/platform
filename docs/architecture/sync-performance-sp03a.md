# SP03a SQLite dependency qualification

Status: implementation frozen for measurement; results pending.

SP03a qualifies the SQLite actually loaded by the packaged Python worker and
Rust executable before SP03b can test capture WAL. Capture stays on DELETE/FULL
with SP02 limits of 32 transactions, 1 MiB and 10 ms. No dependency pin, capture
worker, apply worker, or profiler changes are included. The native executable
adds an explicit installation-verification diagnostic; its bytes change even
though the sync execution path does not.

The reviewed Linux builds are Python SQLite 3.53.1 and bundled Rust SQLite 3.53.2.
Their exact source IDs and required build options are checked against
[the policy](../../components/sqlite-policy.json). SQLite documents the WAL-reset
fix in 3.51.3 and later and selected backports, and its release history identifies
these exact builds. No Linux dependency upgrade is needed.
[SQLite advisory](https://www.sqlite.org/wal.html#walresetbug),
[SQLite release history](https://www.sqlite.org/changes.html).

Build and installed-release checks query each actual runtime, preserve compile
options, and exercise Python DELETE/FULL commit, rollback, integrity and read-only
behavior on private scratch data. The qualification harness interpreter is
recorded separately. An older read-only harness interpreter is not certified as
a live WAL writer. Linux and macOS installed receipts must pass before closure.

## Measurement declaration

Freeze one clean revision and use its unchanged benchmark harness for both arms.
The predecessor is the accepted SP02 diagnostic package; the candidate differs
only in native inspection code and the packaged policy. Preserve package hashes,
source revisions, inventories, all failures and host-monitor observations.

First run nine component trials: three repeats each of the retained 32/10 ms
configuration with a saturated source and reader, 500 transactions/s and reader,
and 500 transactions/s without reader. Each runs 10 seconds, FULL/DELETE, with
bounded pruning. This is a smoke screen, not a configuration search or an
end-to-end speedup claim.

Then run 42 full-stack trials: six candidate instrumentation off/on low-load
controls; twelve fresh SP02 and twelve candidate trials across 4/16 CPUs at
50 rows/s and 8/16 CPUs at 1,000 rows/s; and twelve candidate instrumentation
off/on overload controls at 8/16 CPUs. Use three repeats per configuration,
four clients, two tables of 10,000 initial rows, two changes per transaction,
5 s baseline, 5 s warmup plus catchup, 45 s load and 120 s drain. Use 16 GiB,
no swap or CPU quota, and complete SMT sibling sets. Retain the established
randomized pair order and five-minute quiet-host admission. Repeat contended
pairs; never discard runtime failures or modify competing work.

Investigate a paired median latency, CPU or memory regression above 10%, or any
low-load freshness miss. Near-noise differences do not establish a speedup.
The expected decision is to retain a reliability prerequisite, subject to
platform qualification and the measured no-regression result. Capture WAL
concurrency, checkpoints, sidecars and migration remain SP03b work.
