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
partition. The memory pool, temporary-disk limits, merge predicates, transaction
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
separate frozen workstream.

## Qualification contract

The installed `merge` regression generates 1,888,080 synthetic rows in compacted
Delta storage, inserts 16,384 rows, then performs sparse updates, deletes and a
key move. A commit-before-receipt fault exercises saved-plan replay. Full-field
comparison with duplicate-key detection checks all rows at versions 0, 1 and 2.
Independent processes separate fixture creation, applies, replay and equality
checking; each phase has an external timeout. This directly exercises installed
worker code, not the PostgreSQL capture or Sail query path.

Retained-state replay, analytical worker tests, installed sync suites and the
fresh full SF1 attempt are required before closing #184. Full EQ02 additionally
requires source/Delta equality and all 103 product/reference SQL comparisons.
