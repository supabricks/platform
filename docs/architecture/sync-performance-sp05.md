# SP05 — Maintenance assessment and conditional deferral

Status: **assessment complete; runtime tuning deferred**, 2026-09-26.
SP04 is merged in [#117](https://github.com/supabricks/platform/pull/117) as
`c8e23e26d1d959872f9ac558cf1aa8e0f9e90929`.

The accepted SP04 profiles do not establish checkpointing, pruning or inventory
sealing as the next material bottleneck. Keep the current maintenance policy and
proceed to SP06's source-capacity measurements. Apply the implementation plan's
explicit SP05 deferral path: sustained reclamation, generation rotation and pinned
reader pressure remain required in SP11. This completes the conditional SP05
assessment; it does **not** qualify sustained maintenance or close its follow-ups.

[Reproducible analysis and machine-readable decision](sync-performance-evidence/2026-09-26-sp05/README.md)
cover all 24 existing SP04 main trials, including all 12 candidate trials. These
are historical measurements, not new SP05 trials. No runtime, dependency,
configuration, profiler or measurement harness changed, so there is no new paired
matrix, activation control or speedup claim. A later maintenance change must be
its own SP05 sub-slice with the full fresh predecessor/candidate comparison.

## What the profiles show

Each row describes the three accepted SP04 candidate trials for that cell.
Times and percentages are trial medians. Capture percentages divide inclusive
stage elapsed time by the interval between the first and last profile snapshots
inside load (roughly 44–45 seconds). They are **not CPU utilization**, a prediction
of removable cost, or additive pipeline shares: checkpoints can occur inside
pruning. Full individual results and predecessor comparisons remain in the JSON.

| Logical CPUs / offered changed rows/s | Checkpoint elapsed ms / covered wall | Prune elapsed ms / covered wall | Checkpoint calls | Pruning DELETE calls | Maximum observed spool bytes |
| --- | --- | --- | --- | --- | --- |
| 4 / 50 | 319.10 / 0.73% | 100.74 / 0.23% | 36 | 4 | 4,436,384 |
| 8 / 1,000 | 477.55 / 1.07% | 993.90 / 2.21% | 44 | 44 | 5,935,160 |
| 16 / 50 | 338.57 / 0.77% | 107.56 / 0.24% | 37 | 4 | 4,424,024 |
| 16 / 1,000 | 486.13 / 1.10% | 928.85 / 2.11% | 43 | 43 | 5,971,880 |

Across these 12 trials, observed spool allocation is below 1.12% of the 512 MiB
physical budget; no journal busy or backpressure counter increments are observed.
Every cell exercises repeated checkpoints and pruning DELETEs, but this does not
establish a steady-state plateau. The largest recorded checkpoint call is 21.45 ms
and prune call 50.84 ms, including setup/drain in those lifetime maxima. Progress
reads consume only about 0.03–0.08% of the covered interval. Lowering their polling
frequency is not supported as the next substantial gain by these measurements.

At 8/16 CPUs under overload, median per-worker inventory time is 7.55/7.99 ms,
previous-generation verification 4.63/4.67 ms and retained-boundary checks
6.80/6.85 ms. These inclusive costs overlap with hashing and other work; do not
add them indiscriminately. Complete apply runs take about 307–308 ms, with
141–145 ms in durability and 204–216 ms of imports outside the apply run.
Daemon publication ticks consume about 2.56–2.60 seconds over the covered load
interval, including verification, descriptor and commit work. Those costs warrant
the separate fixed-worker/publication investigation in
[#119](https://github.com/supabricks/platform/issues/119); they do not justify
weakening sync durability or changing checkpoint policy.

The source supplied only about 723–730 changed rows/s in these overload trials.
The previous SP04 comparison measured a 2.3–2.8% input reduction as workers ran
more frequently. This assessment neither explains away that regression nor
attributes its exact cause to maintenance. SP06 must first establish an independent
source-capacity profile under [#107](https://github.com/supabricks/platform/issues/107).

## Coverage gaps and retained policy

There are **zero recorded compaction calls** in the 45-second main trials, across
both arms. The maintenance-base lookup is exercised, but rotation cost is unknown
in this profile. The signed SP04 Linux/macOS maintenance gates separately pass
64-version rollover with a pinned Sail epoch, published-prefix reclamation and
restart, collection after unpin, stopped backup/restore, and source retirement.
Those are functional evidence, not measurements of sustained throughput or tails.
Exact raw reports and source/archive identities remain in
[SP04 qualification evidence](sync-performance-evidence/2026-09-26-sp04/ci/).

[#116](https://github.com/supabricks/platform/issues/116) records a different,
short saturated component experiment: a transient reader left about 14.3 MB
allocated after a later complete PASSIVE checkpoint. That observation remains
bounded by the existing physical admission policy, but the screen did not measure
repeated high-water retention over sustained operation. It supports a future
pressure experiment, not an arbitrary truncation threshold. No issue is closed
and no sustained plateau is inferred from the newer short main trials.

Retain capture WAL/FULL, the sole checkpoint owner, the 4 MiB PASSIVE threshold
and one-second minimum interval, pressure-driven nonblocking TRUNCATE,
conservative physical reservations, pruning only behind the published cursor,
reconnect anchors, and the existing Delta rotation/reader-pin rules. No storage
migration, increased budget or reclamation shortcut is introduced.

## Explicit SP11 handoff

The sustained qualification must retain the frozen source-qualified workload and:

1. Observe at least three successful checkpoint **and reclamation** cycles and
   a real generation rotation per required maintenance run. Counts of requests,
   DELETE statements or maintenance-base lookups alone do not satisfy this gate.
   Extend runs when necessary and archive the actual transition evidence.
2. Repeatedly hold/release bounded SQLite reader snapshots and hold an old Sail
   epoch across rotation and collection. Record reader lifetimes, incomplete/busy
   checkpoints, WAL/DB/sidecar allocation, high-water reuse, retained generation
   sizes and post-unpin cleanup. Pins must not be evicted to pass the test.
3. Predeclare early/late windows and stable-storage/backlog criteria. Record
   checkpoint/prune/compaction timing, p99/max, worst-window p95, physical pressure,
   capture feedback bounds, source input and published throughput. Resource
   bounds and freshness must hold through maintenance, not merely on average.
4. Retain SIGKILL/restart, ENOSPC and recovery checks at maintenance boundaries.
   If a stall, growing allocation, correctness failure or pressure problem is
   reproduced, open/link the issue and isolate one SP05a/b mechanism. Predeclare
   its variant and run the required fresh 24-trial matrix and controls before
   accepting it; do not combine checkpoint and compaction changes.

SP05's disposition is **defer tuning, no performance contribution**. SP11 remains
an open qualification gate. The next required implementation-plan slice is SP06.
