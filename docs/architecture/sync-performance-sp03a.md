# SP03a SQLite dependency qualification

Status: implemented, qualified on both platforms, and measured. Decision: keep for reliability/enabling.

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
these exact builds. No dependency upgrade is needed on either supported platform.
[SQLite advisory](https://www.sqlite.org/wal.html#walresetbug),
[SQLite release history](https://www.sqlite.org/changes.html).

Build and installed-release checks query each actual runtime, preserve compile
options, and exercise Python DELETE/FULL commit, rollback, integrity and read-only
behavior on private scratch data. The qualification harness interpreter is
recorded separately. An older read-only harness interpreter is not certified as
a live WAL writer. Both Linux and macOS installed receipts passed.

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

## Completed component and activation checks

All nine fixed component trials passed without detected external-build overlap.
Median capture throughput was 1,320.54 transactions/s for the saturated reader
case, 499.09 for 500 offered transactions/s with reader, and 499.07 without reader.
These synthetic rates are consistent with SP02 and exclude PostgreSQL, apply and
publication. No grouping setting changed.

All six low-load off/on controls completed correctly below the five-second p95
gate, without detected external-build overlap. Profiling-off median p95 was
3,627.187 ms (3,623.211–3,648.742); profiling-on median was 3,625.217 ms
(3,613.432–3,669.010). Paired median CPU change was +4.65%; paired median peak
memory change was +0.014%. These three pairs are screening controls, not a
latency-equivalence test or evidence of a speedup. Both arms retain the same
imported code; the control measures enabled profiling work.

[Individual controls and accounting](sync-performance-evidence/2026-09-26-sp03a/candidate-controls/comparison.json)
and [component receipts](sync-performance-evidence/2026-09-26-sp03a/component-screen/README.md)
are retained. The overload controls also completed; results follow below.

## Installed platform qualification

The signed Linux x86_64 and macOS arm64 packages both loaded Python SQLite
**3.53.1** (built-in `_sqlite3`) and Rust SQLite **3.53.2**, with exact reviewed
source IDs and `THREADSAFE=1`. Both passed the private DELETE/FULL commit,
rollback, integrity and read-only checks, plus all four installed triggered,
continuous, maintenance and governed sync suites. No dependency upgrade was
needed. This qualifies the dependency prerequisite, not capture WAL behavior.

The separate qualification readers loaded SQLite 3.45.1 on Linux and 3.49.1 on
macOS. Those versions are not classified as fixed by the reviewed advisory.
Current live capture-spool reads use `mode=ro`; stopped corruption fixtures still
use DELETE. This evidence does not authorize either harness to write/checkpoint
a live WAL database. SP03b must qualify any changed connection role.

CI run [36217482375](https://github.com/supabricks/platform/actions/runs/36217482375)
built merge commit `c6c5a0b`, whose tree exactly equals frozen candidate
`da548e7`. Signed package identities, worker inventories, compile options and
actual interpreter hashes are retained in the
[Linux](sync-performance-evidence/2026-09-26-sp03a/ci/linux-installed-sync.json.gz)
and [macOS](sync-performance-evidence/2026-09-26-sp03a/ci/macos-installed-sync.json.gz)
receipts. The local diagnostic package is separate and unsigned.

A broader Linux notebook environment-lifecycle check reproduced the existing
missing-handle failure at environment adoption, tracked in
[#101](https://github.com/supabricks/platform/issues/101#issuecomment-5843561553).
The original failure is retained; it is not a SQLite-gate or installed-sync
failure. Its root cause is still unestablished. The same-head retry passed; both attempts are retained and the issue remains open. All 47 checks passed on the frozen implementation after that retry.

## Fresh SP02 versus SP03a comparison

All 24 accepted trials completed with final table equality and p95 below five seconds. Two contended pairs (four trials) were retained and replaced in full. There were no runtime or cleanup failures.

| CPUs | Offered rows/s | SP02 median p95, ms | SP03a median p95, ms | Paired median p95 change | Paired median CPU change | Paired median memory change |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 50 | 3,623.791 | 3,610.160 | -0.57% | +0.12% | -2.87% |
| 8 | 1000 | 4,583.562 | 4,582.938 | -0.18% | +0.79% | +1.89% |
| 16 | 50 | 3,613.346 | 3,613.005 | +0.85% | +0.08% | +2.68% |
| 16 | 1000 | 4,575.429 | 4,514.604 | -1.96% | +0.53% | -1.72% |

No paired median latency, CPU or memory regression reached the 10% investigation threshold. These three-repeat cells do not establish latency equivalence or a speedup. Percentages are medians of matched-pair percentage changes, not percentages calculated from independent arm medians.

All low-load inputs met their offered rate. Under 1,000 offered rows/s, candidate source delivery was only **685.974–689.969 changed rows/s**, with p95 **4.474–4.589 seconds**. Neither arm met the offered overload rate. Candidate capture medians were about 345.01 transactions/s at 8 CPUs and 343.56 at 16; paired median syncs/transaction changes were +0.29% and +0.88%, and durable-time/transaction changes +0.62% and +0.01%. This is consistent with unchanged capture behavior. The existing source-throughput constraint [#107](https://github.com/supabricks/platform/issues/107) remains; 1,000-row/s and sustained capacity are not qualified.

[Full paired results, ranges and profiles](sync-performance-evidence/2026-09-26-sp03a/main/comparison.json) retain all observations. The native diagnostic executable grew by 54,544 bytes (29,717,096 to 29,771,640); the policy adds 713 bytes. All 26,020 other package inventory entries and files are unchanged. Release-manifest bytes also change to record these two entries. No dependency or worker bytes change.

## Completed overload controls and decision

All twelve overload off/on control trials passed final correctness and the five-second p95 gate, with no detected external-build overlap. These are three repeats per arm at each CPU count.

| CPUs | Off median p95, ms | On median p95, ms | Paired median CPU change | Paired median memory change |
| ---: | ---: | ---: | ---: | ---: |
| 8 | 4,606.892 | 4,522.246 | +4.40% | -2.06% |
| 16 | 4,535.508 | 4,603.596 | +2.95% | +0.75% |

Decision: **Keep — reliability/enabling**. Both packaged production SQLite builds already contain the reviewed WAL-reset fix, and the release gates now verify what actually loads. The fresh comparison found no material regression in this measured profile. No speedup or latency equivalence is established.

The slice retains 24 mandatory trials, 18 activation-control trials, nine component trials and four rejected contended trials. Each rejected pair was repeated in full. The [machine-readable summary](sync-performance-evidence/2026-09-26-sp03a/summary.json), [decision](sync-performance-evidence/2026-09-26-sp03a/decision.json), and [overload controls](sync-performance-evidence/2026-09-26-sp03a/overload-controls/comparison.json) preserve individual ranges and paired accounting.

**Next: SP03b**, the separate capture-WAL/FULL experiment. It must qualify migration, readers, checkpoint ownership, bounded sidecars, restore and rollback on both platforms, then measure its contribution against this retained SP02 grouping behavior. SP03a does not enable capture WAL or broaden supported throughput.
