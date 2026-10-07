# EQ02 — bounded post-compaction merge (#184)

Status: candidate under qualification. SP remains frozen. This document does not
claim a passing full SF1 load or product SQL coverage.

## Diagnosis and rejected alternatives

SF1 attempt 05 stopped on the merge following a successful compaction. Fresh
processes reproduce the stall against an independently reconstructed copy of the
compacted generation. The pre-key-pruning runtime also stalls, excluding the
#182 lookup change as a prerequisite. See the [retained investigation](eq02-key-pruning.md).

The additional controls retain the original source plan and 64 MiB memory /
128 MiB spill-disk limits unless explicitly identified as diagnostic changes:

- Streamed merge execution still times out after 35 seconds.
- Adding target key-range predicates lets the insert-only plan finish in 0.209 s.
- Increasing the merge pool to 256 MiB lets that insert finish in 0.189 s; this is
  a diagnostic control, not a proposed production budget increase.
- Sparse updates at opposite ends of the compacted table still time out with
  either range predicates or an additional exact target-key membership filter.

A range-only correction would therefore leave valid updates blocked. The current
candidate instead removes parallel repartitioning from bounded-memory Delta
sessions. This matches the mechanism described in upstream
[delta-rs #4614](https://github.com/delta-io/delta-rs/issues/4614): FairSpillPool
backpressure interacting with RepartitionExec around a full hash join. Our pin
uses DataFusion 55; the upstream report concerns an earlier dependency version.
This is a supported diagnosis, not a captured native stack proving every detail
of the same internal wait cycle.

## Correction and build contract

The reviewed patch changes only `create_session_state_with_spill_config` in
Delta 1.6.3. When a bounded pool is requested, the session has one target
partition and retains the source-first join order. File-size estimates must not
move the growing target into the non-spilling hash build side. The memory pool, temporary-disk limits, merge predicates, transaction
markers, durability, replay checks and source limits remain unchanged. Unbounded
and disk-limit-only sessions retain the upstream partition setting.

Native assembly now requires the exact upstream commit, reviewed patch, original
published source-distribution Cargo lock, pinned Rust/Maturin builder, wheel
hash, build receipt and dependency notices. Both native targets build from these
inputs. An upstream wheel with the same package version is rejected. This keeps
the runtime correction explicit and reviewable without silently resolving a new
dependency graph.

The tradeoff is reduced parallelism within a bounded merge. Measurements below
must establish correctness and progress; SP throughput qualification remains a
separate frozen workstream. The controlled Linux build also increases the Delta
native extension from 127.35 MiB to 190.84 MiB (63.49 MiB) with its explicit
non-LTO build profile. These are extension bytes, not total installation or
compressed archive sizes; package-size optimization is not claimed here.

## Qualification contract

The installed `merge` regression generates 1,888,080 synthetic mixed-width rows in one compacted
Delta file (including four padded CHAR columns), inserts 16,384 rows, then performs sparse updates, deletes and a
key move. A commit-before-receipt fault exercises saved-plan replay. Full-field
comparison with duplicate-key detection checks all rows at versions 0, 1 and 2.
Independent processes separate fixture creation, applies, replay and equality
checking; each phase has an external timeout. This directly exercises installed
worker code, not the PostgreSQL capture or Sail query path.

Retained-state replay, analytical worker tests, installed sync suites and the
fresh full SF1 attempt are required before closing #184. Full EQ02 additionally
requires source/Delta equality and all 103 product/reference SQL comparisons.


## First candidate evidence (single partition only)

The reviewed Linux wheel built successfully. The unsigned engineering overlay
has identity `7713ff1bb0b7abf530faaba7f35a748bc69abc52c02574f933a642bfac0f1770`.
It passes all **147 analytical worker tests**, **75 packaging tests**, **20 component
tests**, and seven installed suites / **25 checks**. The installed suites include
bulk, DATE, CHAR, composite, continuous, maintenance and the bounded merge fixture;
all supervisors report zero leaked/remaining descendants. Continuous sync meets
its existing 50-row/s and five-second p95 gate. This is not a new SP qualification.

The original retained insert plus exact changed-row/count validation takes
**0.563 s**, with **492.56 MiB** kernel high-water RSS. Sparse updates at the ends
of the retained 1,838,080-row table complete a full-file rewrite and validation in
**0.472 s**, with **330.40 MiB** high-water RSS. No target range predicate or pool
increase is used. These are isolated replays, not full ingestion throughput.

The regression development controls are retained too. An initial expected-value
check widened int32 to int64 and failed; corrected all-integer fixtures pass both
runtimes, even with one file, so they are insufficient as a regression detector.
The final mixed-width fixture writes a 13,223,685-byte file: the old runtime times
out after 90 seconds during its insert; the patched runtime completes all three
checks including sparse mutations, committed-plan recovery and exact old versions.
The outer supervisor reports a normal failed test exit after the child timeout,
not an outer-supervisor timeout, and zero leaked processes.

SF1 attempt 06 fails after 38.340 seconds, with 221,008 committed rows and an
observed 155,472-row publication, on `incremental_worker_failed`. A fresh-process
replay reveals a bounded `HashJoinInput` allocation failure (63.7 MiB reserved;
an additional 683.4 KiB cannot fit in the unchanged 64 MiB pool). The physical plan
puts the existing multi-file target on the build side after optimizer reordering.
Thus the first candidate removes the observed deadlock but is insufficient for
full loading; it must not be promoted as the complete correction.

The revised candidate additionally disables statistics-driven join reordering in
bounded sessions, keeping the source on the build side. A second fixture covers
many small files whose compressed size misleadingly suggests a cheap hash build.
The revised candidate passes the retained failure replay (44 ms merge execution),
the original stalled insert and sparse-update replays, and the expanded four-check
regression. Its broader installed and full SF1 qualification is in progress. Exact Linux/macOS archive CI
also remains required; the first CI run encountered a separate HTTP 502 fetching
Unity Catalog build inputs.


The revised installed runtime has identity
`9f825f1381f660751f7815d4baf7b39d0d8e631049c4b31640e634651272b988`.
All 147 worker tests and seven installed suites / 26 checks pass on it, with
zero leaked/remaining descendants. The mixed-width compacted fixture takes
0.435 s for insertion and 0.368 s for sparse mutation; the small-file fixture
takes 0.162 s and 0.054 s respectively. These direct regression timings exclude
fixture creation and full equality checks and are not ingestion throughput.

[Public evidence](tpcds-evidence/2026-10-07-eq02/bounded-merge/summary.json) retains
both source-build receipts, engineering package proofs, all fixture controls,
worker/installed test logs, retained failure replays, before/after physical plans
and the failed SF1 attempt 06. Parent `SHA256SUMS` covers these files. Input/mailbox
configuration and databases remain private. Attempt 07 is still running; no
passing full-load or product-query claim is made by this evidence snapshot.
