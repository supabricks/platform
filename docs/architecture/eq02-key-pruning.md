# EQ02 — bounded key lookup implementation and SF1 rerun

Status, 2026-10-07: **#182 correction implemented and tested; full SF1 qualification
blocked by [#184](https://github.com/supabricks/platform/issues/184)**. Keep #182
open pending complete qualification. SP remains frozen.

## Change and regression coverage

Commit `e36f07c408a274048b631ac8d5c339831132b732` implements the correction from
the [memory investigation](eq02-apply-memory-investigation.md). Every primary-key
column now has inclusive minimum/maximum bounds alongside its membership filter,
letting Parquet statistics reject disjoint row groups. Composite candidates still
undergo exact tuple filtering. Scanner read-ahead is one batch/fragment with
threading disabled; the existing 32-row batch bound remains.

Memory budgets, complete-transaction publication, previous-version reads,
mutation/deadline checks, saved-plan replay and Delta merge behavior are unchanged.
Empty key sets select no rows. New real-Parquet tests cover large nonmatching
ranges with physical row-group pruning, sparse unsorted keys, missing statistics,
signed integer extremes, and wide composite Cartesian neighbors.

All **147 analytical worker tests pass**, including mutation guards, key moves,
deletes, crash/replay and compaction. The verified unsigned engineering overlay
has identity `94c7b20815224b06645167396316ba41fedc35abe0669def2ef30beb31ff42f3`.
Its package proof checks all 26,042 base payload files and changes only the
worker source and its checked-hash bytecode.

Six installed suites pass: **bulk (2 checks), DATE (4), CHAR (4), composite (3),
continuous (4), maintenance/recovery (5)**. They include exact source/Delta
equality, pinned Sail readers, capture/daemon restart, 64-version compaction,
journal pruning and stopped backup/restore. Every supervisor reports zero
leaked/remaining descendants. These exercise an installed engineering overlay,
not an exact signed-release archive. Linux/macOS archive CI is a separate gate.

Three fresh-process replays of the original failed range use the packaged worker
without replacing its filter or scanner. All preserve the exact original plan
hash and end LSN. Plan/apply takes **0.653–0.700 seconds, median 0.666 seconds**,
with **475.3–527.7 MiB kernel peak RSS**. The prior baseline's corresponding
median was 8.208 seconds, with peaks of 713.5–785.8 MiB. These are instrumented
retained-state comparisons; they do not measure full ingestion throughput.

## SF1 attempt 05

The same immutable dataset, loader and publication window were used: 24 native
tables / 19,557,335 intended rows, continuous sync healthy before writes,
1,024-row / 4 MiB COPY transactions, and a 65,536-row unpublished window.
The container uses CPUs 0–7, 16 GiB with no swap/network, and second-NVMe storage.
An external observer samples owned apply workers every 100 ms; no worker code,
memory gate or source transaction is changed for observation. Short-lived
workers and final peaks can be missed, so sampling is not a hard memory bound.

| Result | Observation |
| --- | --- |
| Committed rows | 1,953,616 |
| Last published rows | 1,888,080 |
| Elapsed including startup/cleanup | 539.224 s |
| Flow-control waiting | 486.599 s |
| Observed apply workers | 46 |
| Highest observed kernel high-water RSS | 677.668 MiB |
| Sampling errors | 0 |
| Final WAL / journal bytes | 2,480 / 13,698,936 |
| Supervisor descendants / leaked / remaining | 103 / 0 / 0 |
| Terminal state | `resync_required: triggered_run_fenced_or_expired` |

The run passed the old memory-failure point and did not encounter a memory-budget
failure. It then stalled at the apply following the second compaction. Compaction
had already completed in 2.328 seconds, producing 26 files / 19,844,691 bytes for
1,888,080 rows. The saved plan contains 16,384 customer-demographics inserts,
from Delta version 0 to the intended version 1, ending at LSN `0/11269818`.
No version 1 appeared. Worker CPU remained at 4.55 seconds while native threads
waited on futex/epoll. The existing request deadline eventually fenced the run.
These live observations are retained separately from worker-generated receipts.

## Isolated stall and disposition

[#184](https://github.com/supabricks/platform/issues/184) tracks this blocker.
The failed unpublished generation was removed by normal cleanup. The immutable
previous generation and privately copied input/plan survive, allowing compaction
to be reconstructed on independent diagnostic state.

The diagnostic matrix preserves every attempt:

1. Direct copy of the failed new generation fails because cleanup removed it.
2. Reconstructed compaction completes, then candidate `TableMerger.execute()`
   stalls; a 45-second external timeout terminates it.
3. A fresh process applying directly to those reconstructed files also stalls.
4. The **pre-fix row-prefix runtime** applying to identical copied files also
   stalls at the same merge boundary.

Faulthandler records the merge boundary every ten seconds. Thus the isolated
stall does not require worker reuse, compaction in the same process, or the new
key-lookup implementation. These observations do not yet identify the underlying
Delta/DataFusion cause. The timed-out diagnostic result files retain `RUNNING`
because termination prevents their finalizer; external termination receipts
record exit 124 and take precedence. None is a passing replay.

Fix and qualify #184 before repeating SF1. A complete load must then pass exact
source/Delta verification and all 103 product/reference analytical comparisons.
**Current full product SQL coverage remains 0/103.** Neither #182 nor EQ02 is
closed by crossing the earlier failure point alone.

[Evidence](tpcds-evidence/2026-10-07-eq02/key-pruning/summary.json) includes package
proof, tests, six installed-suite receipts, three range replays, failed load
ledgers, sampled memory, live/final run records, diagnostic scripts and timeout
logs. Parent `SHA256SUMS` covers public artifacts. Private state remains under
`/data2/supabricks-eq/eq02-20261007/`; input/mailbox configuration and databases
are not committed. The retained scripts record exact container/mount commands.
