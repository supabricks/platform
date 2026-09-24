# Local synchronization CPU scaling

[Documentation home](../README.md) · [SY08 release evidence](sy08-installed-sync.md)

## Question and scope

How much does the current PostgreSQL → analytical publication pipeline benefit
from more CPU cores on the same machine? Measure that baseline before changing
worker concurrency, batching, or resource budgets. Source write rate is an offered
load, not proof of replication capacity. The existing 50 changed rows/s test and
five-second p95 gate do not establish a maximum throughput or fixed lag.

This is a local screening experiment on a shared development machine. It does not
establish an EC2 instance recommendation, a product SLA, or installed release
qualification. The runtime is held constant throughout the matrix.

## Method

The [reproducible harness](../../e2e/native/performance/README.md) runs sequential,
randomized, disposable trials with three repeats per load/configuration pair.

| Variable | Configuration |
| --- | --- |
| Host | AMD Ryzen 7 7800X3D, 8 physical cores, 16 logical CPUs, approximately 62 GiB RAM |
| CPU profiles | 4, 8, 16 logical CPUs; respectively 2, 4, 8 complete SMT sibling pairs |
| Affinity | `0,1,8,9`; `0,1,2,3,8,9,10,11`; all 16 logical CPUs |
| Memory | 16 GiB for each entire trial container; container swap disabled |
| CPU quota | None; CPU affinity alone restricts execution |
| Storage | Fresh directories on the same local NVMe ext4 filesystem |
| Data | Two tables, each with 10,000 integer primary keys and integer values |
| Source workload | Four independent clients; each transaction updates one row in both tables |
| Offered loads | 50, 250, 1,000 changed rows/s (25, 125, 500 transactions/s) |
| Trial phases | Five-second source baseline without sync, reset values, bootstrap, five-second warmup, 45-second measurement, bounded drain, final data comparison |
| Sync configuration | Unchanged default continuous policy: 500 ms batch interval, 5,000 ms freshness target, production apply/retention budgets |
| Repetitions | Three per pair, randomized with seed `20260923` |
| Isolation | One stack per container; package/repository read-only; no external network |

Clients own disjoint keys, avoiding same-row source lock contention. Offered work
is paced against a shared clock. Work not submitted before the measurement window
ends is counted as unsent; completed transactions, achieved rate, source latency,
and submission lateness are retained. Compare the source-only baseline before
attributing a missed input rate to synchronization. Five seconds is only a short
source baseline, not a separate saturation test.

For every committed transaction, the observer maps its PostgreSQL transaction ID
to its durable capture end LSN, then to the first atomic analytical publication
covering that LSN. End-to-end lag is publication time minus client-observed COMMIT
acknowledgment time. Both clocks are on the same host. This excludes time between
server commit and client acknowledgment; it does not measure a subsequent Sail
query or query startup. Source request latency is reported separately.

Stage distributions cover acknowledgment → batch admission → worker start →
prepared descriptor → atomic publication. They are weighted by measured source
transactions, not equally by batch. Their individual percentiles cannot be added
to reconstruct an end-to-end percentile. Capture observation is an upper bound
with a nominal 100 ms polling interval and additional SQLite busy retries.
Published timestamps come from the durable publication record, not poll time.

After writes stop, all measured transactions must publish within 120 seconds and
both full analytical tables must equal the frozen PostgreSQL source. Missing
transaction markers invalidate the measurement. Runtime policy failures and
drain timeouts remain failed trials; incomplete latency samples are never
presented as a complete p95. Successful attribution and correctness do not turn
a latency or offered-rate miss into a performance pass.

Resource evidence includes cgroup CPU seconds per elapsed second (average cores
used), quota throttling, memory, I/O counters, pressure counters, and sampled
capture backlog. CPU use includes the native stack and harness. Whole-host CPU
counters help identify interference. Cgroup memory includes page cache; it is
not equivalent to process RSS. Backlog samples may be stale between capture
status updates and do not substitute for transaction publication accounting.

## Limits

SMT threads are not physical cores. Affinity does not reserve these CPUs from
other host applications or reproduce EC2 scheduling, CPU frequency, memory
bandwidth, EBS, or network storage. Shared cache and filesystem page cache remain
enabled; repeated randomized trials reduce order bias but do not eliminate it.
No global caches are dropped and no unrelated services are stopped.

These narrow-row updates over a small data set do not cover wide rows, large
tables, inserts/deletes, many tables, many projects, long transactions, analytical
query interference, recovery, or long-running retention/compaction. Forty-five
seconds is a screening window, not a steady-state soak. Three trials give a
range, not a strong statistical confidence interval. A summary of trial p95s
must be labeled as such; it is not a pooled transaction p95.

## Follow-up sequence

Tracked findings: [capture throughput and CPU scaling
(#87)](https://github.com/supabricks/platform/issues/87), and [incremental worker
failures requiring resync (#88)](https://github.com/supabricks/platform/issues/88).
Their initial measurements are provisional until the full matrix is archived.

The current implementation has several distinct serial boundaries:

- [`Spool.append`](../../python/analytics/capture/spool.py) durably commits each
  captured source transaction using SQLite's rollback journal and `synchronous=FULL`.
  [`capture_worker.py`](../../python/analytics/capture_worker.py) acknowledges only
  that durable cursor. Any group-commit experiment must preserve complete source
  transactions, replay checks, contiguous cursors, disk budgets, and crash-safe
  acknowledgment; turning off durability is not an optimization candidate.
- [Incremental admission](../../crates/local/src/store/incremental.rs) allows one
  active writer per installation. Independent projects therefore cannot assume
  concurrent apply merely because more CPUs are available.
- [`incremental_worker.py`](../../python/analytics/incremental_worker.py) applies
  tables sequentially before preparing a single group descriptor. Parallel table
  work must remain bounded and publish only after every table succeeds.

These code boundaries suggest experiments; measurements determine their order.

1. Preserve the current implementation's complete baseline, including overload
   and failure cases. Identify the limiting stage and whether source generation,
   capture, publication, or apply fails to keep up.
2. Address the demonstrated bottleneck. Evaluate bounded batching and worker
   reuse where setup or durable I/O dominates; evaluate bounded table parallelism
   where apply is CPU-bound. Avoid increasing all thread pools indiscriminately.
   Diagnose generic `incremental_worker_failed` results with private, bounded
   instrumentation before assigning a cause. In particular, distinguish a busy
   spool reader from decoding, schema, storage, and apply failures. A capture
   group-commit experiment must retain FULL durability and acknowledge only after
   the entire bounded group commits.
3. Replace the installation-wide single-writer restriction only with explicit
   per-source/generation ownership, global resource admission, and fencing.
   Preserve ordered atomic publication across a transaction's tables.
4. Repeat the exact matrix with new binary/script identities and compare all
   repeats, including correctness, source impact, memory, and write amplification.
   Add multi-project and many-table workloads for concurrency changes.
5. Run longer saturation/soak trials, recovery and pinned-reader qualification,
   then verify actual cloud instance families before publishing sizing guidance.

The existing SY08 gates remain unchanged. Performance work does not waive
transaction atomicity, durable acknowledgment, authority, fencing, retained-reader
correctness, or bounded resource use.
