# SP10c evidence review — October 5, 2026

**Decision: retain SQLite; do not adopt RocksDB.** The available experiment does
not demonstrate an end-to-end benefit. Retain the separately measured execution
history (#156) and capture maintenance (#157) corrections for reliability, with
no short-trial speedup claim. The review of available evidence is complete;
**SP10c qualification is not complete**. Two planned sustained comparison arms
remain missing, and release CI was still running when this review was recorded.

## Evidence verified

The read-only reviewer checks original artifact hashes, frozen harness source
hashes, runtime/binary identities, workload and resource envelopes, quiet-host
receipts, cleanup, final correctness and accepted-pair membership. It reruns the
frozen comparison validator and reconstructs paired summaries from the retained
per-trial metrics. It does not independently recover every transaction timestamp
from the deleted private fixtures or recompute latency quantiles from those
unretained timestamps.

- Sequence 02: **324 accepted fixtures** across observer bridge, engine attribution
  and product comparison: 216 pipeline trials plus 108 component, lifecycle and
  observer-control fixtures. There were 326 executions: one contended observer
  pair was rejected, retained, and replaced. All accepted pipeline trials were
  measured and fresh; historical four-client 1,000-row/s cells missed offered
  input and remain excluded from capacity claims.
- Corrections: **24 accepted pipeline trials**, twelve each for #156 and #157,
  all correct, fresh, input-qualified, and without contention or cleanup leaks.
- Corrected SQLite owner: **one accepted 30-minute run**, after two retained
  failed sustained attempts. Those failures have no accepted freshness or final
  equality result and are never averaged into successful measurements.
- All six exported evidence bundles reproduce the original review's metrics and
  trial dispositions. Inner manifests retain original and sanitized hashes;
  the outer manifest covers bundles, reviewer, structured results and decision.

The comparison harness is frozen at `3fdebb011892a3d97bd5d1b22d9e74c9538a661e`;
correction qualification uses `c3c771df5c37b43aa3d92686adb4499229c19f58`.
Later CI fixes did not modify these harnesses or installed measurement packages.

## Matched engine and product results

Three fresh pairs per cell; eight source clients; 1,250 changed rows/s offered.
Each source transaction updates one row in each of two 10,000-row integer tables.
Main comparisons have profiling enabled on both arms. CPU counts are the allowed
logical-CPU affinity, not dedicated physical EC2 instances. These paced trials do
not find maximum sustainable throughput or establish statistical equivalence.

| Comparison | CPUs | Median p95, SQLite → RocksDB | Paired p95 change | Paired CPU change | Paired cgroup peak-memory change |
|---|---:|---:|---:|---:|---:|
| Same owner interface | 8 | 3,038.155 → 3,211.891 ms | +6.846% | +1.654% | +0.138% |
| Same owner interface | 16 | 3,053.663 → 3,137.077 ms | +2.724% | +2.012% | −0.446% |
| Direct SQLite → full RocksDB owner | 8 | 3,037.065 → 3,229.588 ms | +6.339% | +2.355% | +0.179% |
| Direct SQLite → full RocksDB owner | 16 | 3,025.827 → 3,169.515 ms | +5.036% | +3.343% | +0.465% |

All qualified main arms delivered about 1,249–1,250 changed rows/s and passed
five-second p95. The three p95 regression screens above 5% are recorded in
[decision.json](decision.json) and [#163](https://github.com/supabricks/platform/issues/163).
Paired p99 costs were +10.51%/+6.16% for the owner comparison and +8.81%/+8.35%
against direct SQLite. Paired percentages are medians of within-pair changes;
they need not equal the percentage change between the displayed arm medians.

RocksDB improves isolated 32-record owner-read latency by 15.01%, but worsens
512/4,096-record reads by 8.93%/4.12%. Against direct SQLite, its full owner read
latency costs 170.27%/481.82%/284.17% for those sizes. Sixteen requests within one
fixture are correlated observations, not sixteen independent trials. No component
result justifies adopting a slower full pipeline.

The observer bridge passes the main investigation screens: qualified p95 paired
changes are −0.323%/+0.677%, and CPU −0.288%/0.000% at 8/16 CPUs. Marker observer
activation adds about 1.05%/1.66% CPU in its controls. RocksDB profiler activation
adds about 7–8% CPU and also affects lag; therefore the instrumented deltas are
not a precise estimate of uninstrumented production overhead. We do not subtract
control overhead or unrelated stage percentiles from the main result. Missing
SQLite COMMIT counters in RocksDB are unavailable metrics, not zero-cost durability;
RocksDB synchronous WriteBatch counters remain separately represented.

## Correction attribution and sustained result

| Correction | CPUs | Paired source-rate change | Paired p95 change | Paired CPU change |
|---|---:|---:|---:|---:|
| #156 bounded private execution history | 8 | −0.180% | +0.196% | +0.291% |
| #156 bounded private execution history | 16 | −0.038% | −0.773% | 0.000% |
| #157 bounded capture maintenance | 8 | −0.593% | −0.170% | 0.000% |
| #157 bounded capture maintenance | 16 | −0.005% | −0.784% | +0.200% |

Neither correction crosses the investigation screens (source −5%, p95 +5%, CPU
or peak memory +10%). Their contribution is sustained correctness and bounded
private history, rather than increased short-trial throughput. The short paired
trials alone do not isolate every effect of the combined corrected soak.

The corrected SQLite-owner run used eight allowed logical CPUs, 16 GiB memory,
no swap, eight clients, 60 seconds of warmup and 1,800 seconds of offered load:

| Measure | Result |
|---|---:|
| Actual committed changed rows/s | 1,249.877 |
| Completed source transactions | 1,124,904 of 1,125,000 |
| Commit-to-publication p50 / p95 | 2,507.103 / 4,269.021 ms |
| p99 / maximum | 5,032.559 / 6,069.955 ms |
| Final drain | 2.827 seconds |
| Average whole-cgroup CPU | 1.178 cores |
| Peak cgroup memory (includes page cache; not RSS) | 3,727,273,984 bytes |
| Peak observed unpublished backlog | 491,436 bytes |
| Sampled capture spool high-water | 5,623,984 bytes |
| Recorded capture backpressure events | 0 |
| Published artifacts | 1,126 |
| Retained incremental runs / requests / automatic parents | 742 / 742 / 742 |

Both final tables equal the frozen PostgreSQL source. Catalog foreign keys pass;
the journal reopens with captured LSN 324410920, retained prefix 324392912,
9,097 logical retained bytes and 4,325,552 physical bytes. All 131 observed owned
descendants were cleaned up, with no remaining or leaked descendants.

The independent sampler has 1,845 samples (1,767 during measurement), no inspection
denials, and a maximum sample gap of 3.56 seconds. Capture's sampled peak RSS is
49,586,176 bytes. Per-role resource counters are in [sustained.json](sustained.json).
Sampling can miss short-lived processes and peaks. Kernel process write-byte
counters are not device write amplification; this run does not establish a
steady-state bound on whole-stack storage, long-duration memory or reader pinning.
The >5-second p99 is reported rather than hidden by the passing p95 target.

## Remaining gates and next action

1. The planned **RocksDB-owner and direct-SQLite 30-minute arms have not run**.
   Do not apply the successful corrected SQLite-owner result to those packages.
   Completing the original experiment requires fresh, explicitly frozen packages
   with the relevant reliability corrections, matched resource observation and
   compaction/pruning evidence. This review does not silently waive those gates.
2. Retain SQLite as the current engine. SP11 still needs repeated 15/60-minute
   trials, early/late backlog and memory bounds, capacity sweeps, actual maintenance
   cycles, pinned readers, rotation/GC, pressure, crash and recovery evidence.
3. SP12 requires exact installed Linux/macOS and governed qualification and a
   scoped supported envelope. CI run [37354428991](https://github.com/supabricks/platform/actions/runs/37354428991)
   on `1aa3f3a` was pending at review time. Passing CI alone would not complete
   SP11/SP12. The prior intermittent governed denial (#137) remains unresolved.
4. The planned TPC-DS end-to-end analytics work follows the SP workstream. This
   two-table update fixture is not a TPC-DS, data-load, growing-dataset or analytical
   query benchmark, and provides no dataset-size scaling claim.

## Reproduction and retained inputs

From this directory, using Python 3.11+ on Linux:

```sh
sha256sum -c SHA256SUMS
python3 reproduce.py
```

Reproduction extracts only regular files into temporary directories, verifies
inner hashes and frozen validator source, revalidates every accepted and rejected
receipt, and compares all metrics/trial dispositions with the original review.
Original/local hashes differ from sanitized-export hashes by design; those hash
fields alone are excluded from semantic equality. `export.py` records how the
six bundles were produced from the original immutable local roots; it refuses
existing staging destinations. Failed sustained reports/resources are retained in
`failed-sustained.json.gz`, with original hashes and their failure receipts.
Private database scratch and logs are excluded. No benchmark was rerun by this review.
