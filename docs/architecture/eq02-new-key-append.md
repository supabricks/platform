# EQ02: append when the saved plan proves new keys

Status: [#189](https://github.com/supabricks/platform/issues/189) correction passes
153 worker tests, the retained failing batch, and the large installed regression.
Additional installed sync suites are running.
[Retained evidence](tpcds-evidence/2026-10-07-eq02/new-key-append/summary.json). Complete SF1 qualification remains
pending; SP stays frozen.

## Failure and attribution

Attempt 09 includes the publication scheduling correction (#186). It reaches
13,630,753 committed / 13,565,217 published rows in 2,943.160 seconds before the
768 MiB apply-worker limit fences capture. Cleanup observes 505 descendants and
leaves zero. The preceding complete publication remains intact. The failed apply
rolls over a generation whose inventory table contains 11,010,048 rows.

A fresh diagnostic process replays the retained immutable generation and journal
into a new private directory. Compaction takes 64.45 seconds and peaks at
419,860,480 bytes. Planning adds no new high-water mark; merge raises it to
827,564,032 bytes. The diagnostic disables the individual worker kill so the
phase can finish; this does not raise any production or SF1 qualification limit.

Delta's merge barrier keeps batches for unchanged target files in a vector with
no pool reservation or spill accounting. Those allocations sit outside the
64 MiB merge pool. Source-first join ordering and a single target partition
remain necessary for #184, but do not bound this barrier. A fresh merge-only
process peaks lower (668,848,128 bytes), demonstrating why allocations retained
from compaction matter to this failure.

## Correction and scope

Implementation: `c748ab7727633d17e59404ff0cfd381cf47be5e3`.

The production planner already looks up the exact changed keys at the pinned
Delta version and saves the final transaction overlay plus its row-count delta.
Each final key contributes at most one added row. When the row delta equals the
number of final rows, and none is a deletion, every final key is proven absent.
Only this case uses Delta append. Existing-key changes, deletes, mixed batches
and plans lacking this proof retain merge.

Append restores the exact target Arrow schema, including nullability and
metadata. It uses the existing run/plan commit markers, version checks,
commit-before-receipt replay, file durability checks and atomic publication.
No source rows, memory limits, deadlines, spill budgets or backlog bounds change.
The prior merge regression deliberately lacks the proof and still exercises
actual bounded merge. General large-table mutation memory remains separately
tracked in [#190](https://github.com/supabricks/platform/issues/190).

## Measurements and correctness

| Fixture | Predecessor | Append candidate |
| --- | ---: | ---: |
| Retained full compaction/plan/apply high-water | 827,564,032 B | 435,208,192 B |
| Retained full worker elapsed | 67.144 s | 66.958 s |
| Synthetic 16,777,216-row target, planner/apply high-water | 1,029,730,304 B | 385,003,520 B |
| Synthetic worker memory assertion | Fails 768 MiB limit | Passes |

These are diagnostic phase measurements, not full PostgreSQL sync throughput.
Compaction dominates the retained full-worker duration; the candidate apply
phase takes 42 ms. The candidate synthetic fixture adds 16,384 composite-key
rows, faults after commit, replays the saved plan without another commit, and
checks every row and value in both Delta versions. It is a mandatory installed
Linux/macOS archive gate. The first candidate fixture run passed correctness
but omitted metrics from its report; the corrected second run supplies the
measurements above. Both attempts and the predecessor failure remain retained.

All 153 worker tests pass, including a new-key transaction overlay with exact
decimal data, a multi-table crash/replay, mixed existing-delete/new-insert merge,
and missing-proof fallback. Installed-suite and full SF1 results will be added
as each completes. No complete product query or release qualification is claimed.
