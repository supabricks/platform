# SP00 — Reproducible synchronization comparisons

The supported paired runner reproduced the original runtime's main limitations
across **24 mandatory trials**. Both harnesses completed all six low-load trials
correctly and failed all six overload trials. One candidate low-load trial missed
the five-second freshness target. Its profile identifies a 2.43-second successful
journal read; a predeclared six-trial follow-up passed all correctness, input and freshness
checks. **Keep — reliability/enabling:** SP00 supplies reproducible comparisons,
not a runtime optimization or throughput improvement.

[Raw evidence and reproduction](sync-performance-evidence/2026-09-24-sp00/README.md) ·
[Measurement contract](sync-performance-comparisons.md) ·
[Implementation plan](../plans/sync-performance-implementation.md)

## What changed and what was held fixed

The new runner accepts predecessor/candidate package and harness inputs, explicit
CPU/rate cells, balanced seeded pair order, immutable identities and validated
resume. It monitors known external builds and descendants, requires five rolling
quiet minutes before each trial, retains and replaces an entire contended pair,
and stops on measurement or cleanup errors. Reports preserve failure denominators,
input shortfalls, incomplete profile tails and artifact hashes. Export redacts
local paths and excludes private logs/scratch; raw profiles remain available.

The predecessor harness is `bc039090c609560dfb9ad676e339229f4e65e16f`; the measured
candidate is `f0a93ed0ac91d4eaafc114319fc21ec1ca0eeb01`. Both use runtime source
`92272759ad06af6207b9537a02871f0477e17295`, diagnostic binary SHA-256
`29d70940c6a6c88c36f3a3c90cdc826d7a5f38ec1797083968b962d296809156` and installed
manifest `5e06525d48333da3a5b4c0587eb2a0a085d626dea3a1eca99cee28dfc152286e`.
The source revision is an operator assertion; this is not a signed release.

Only `matrix.py` changed among the existing tracked native qualification/installer
Python files. Trial, workload, cleanup and profiling code are byte-identical;
new orchestration/report modules execute outside the measured runtime. The same
host monitor surrounds both arms. There is no new runtime probe activation in
SP00. The original archive and its activation controls remain unchanged. Final
export redaction, tests, CI wiring and this report were added after the measured
harness was frozen; they do not alter the trial or its recorded outcomes.

The mandatory protocol is unchanged: 4/16 logical CPUs at 50 offered rows/s,
8/16 at 1,000, three repeats each per arm, four source clients, two 10,000-row
tables, two changed rows/transaction, 5-second source-only baseline, 5-second
warmup plus catchup, 45-second load and 120-second drain. Each fresh fixture has
16 GiB memory, no swap/quota, complete SMT pairs and the same immutable qualifier
image. This constrains a local Ryzen 7800X3D/NVMe/ext4 stack, not an EC2 instance.

## Mandatory matrix results

P95 entries are trial median [minimum, maximum], in seconds. Completion means
full-table correctness and complete transaction attribution; freshness is separate.

| Logical CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 | Candidate p95 |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3.740 [3.594, 3.743] | 3.702 [3.580, 3.719] |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 2/3 | 3.654 [3.621, 3.665] | 3.817 [3.709, 5.366] |
| 8 | 1,000 | 0/3 · unavailable | 0/3 · unavailable | unavailable | unavailable |
| 16 | 1,000 | 0/3 · unavailable | 0/3 · unavailable | unavailable | unavailable |

Low-load source rates met their target in every trial. Median p95 changes are
−1.02% at four CPUs and +4.46% at sixteen; these small samples do not establish a
speedup or statistical equivalence. Median container CPU changes are −2.01% and
+0.72%, and peak memory changes +0.13% and −1.19%, respectively. The freshness miss
requires investigation despite the modest median change.

The predecessor's six overload failures reproduce the original mix: four
resync-required errors and two drain timeouts. The candidate has six
resync-required errors, three during warmup and three during drain. Profiles
identify the same three-second `SQLITE_BUSY` read boundary; which failure occurs
first depends on capture/read timing in fresh fixtures. The code and profiler are
unchanged, but these few runs do not establish identical failure-time distributions.
Early failures are retained and explain the missing load-phase observations.

Available overloaded capture windows remain approximately 29–30 transactions/s,
29.83–30.93 ms/COMMIT and exactly four native sync calls/COMMIT. Predecessor capture
ranges use six trials; candidate ranges use three because three failed in warmup.
Selected successful apply workers still spend roughly 1.04–1.18 seconds in
boundary scans versus 18–20 ms in Delta merge. These are component observations
inside failed pipelines, not successful end-to-end throughput.

The source-only 1,000-row/s baselines achieve 843–882 rows/s; measured load reaches
649–672 rows/s where available. Thus the four-client workload still cannot supply
the 1,000-row/s acceptance target, independently of the capture bottleneck.
Historical low-load medians also differ from this fresh block; use the fresh
paired predecessor for attribution rather than crediting drift as improvement.

## Investigated freshness miss

In candidate 16-CPU repeat 2, p95 is 5,365.567 ms and p99 is 6,269.273 ms. Worker
`incremental-4173` spends 2,430.136 ms in SQLite SELECT and 2,430.484 ms in journal
reading, extending its apply run to 3,367.416 ms. It succeeds before the existing
three-second busy timeout. The following workers spend about 1.08–1.11 seconds in directory boundary checks. Capture
COMMIT cost remains near its low-load baseline and the cgroup is not throttled.

This is an existing reader/writer contention mechanism, now observed as a
freshness miss without an exception. [Issue #96](https://github.com/supabricks/platform/issues/96)
tracks it alongside the fatal-read [issue #88](https://github.com/supabricks/platform/issues/88).
Bounded retries alone will not remove this tail. The original result stays in
the mandatory matrix; the follow-up repeats the unchanged 16:50 cell three times
per arm with the original order balance reversed. It is additional diagnostic
evidence, not a replacement acceptance matrix.

## Retained follow-up and combined result

The [additional block](sync-performance-evidence/2026-09-24-sp00/low-load-followup/comparison.json)
completed all six trials with correctness, input rate and freshness passing. It
took 13.99 minutes including another initial quiet wait, with no replacements or
cleanup failures. Combined with the original 16:50 cell, each arm ran first in
three pairs and second in three pairs.

The [combined six-pair analysis](sync-performance-evidence/2026-09-24-sp00/low-load-combined.json)
retains every original outcome:

| 16 CPUs / 50 rows/s, six trials per arm | Predecessor | Candidate |
| --- | --- | --- |
| Correct completion / input target | 6/6 · 6/6 | 6/6 · 6/6 |
| Five-second p95 freshness | 6/6 | 5/6 |
| P95 trial median [range], seconds | 3.659 [3.621, 4.076] | 3.836 [3.709, 5.366] |
| P99 trial median [range], seconds | 3.850 [3.824, 4.639] | 4.302 [3.894, 6.269] |
| Median average CPU cores | 1.249 | 1.241 |

Combined p95 is +4.84% and p99 +11.76%. The p99 change exceeds the plan's 10%
investigation threshold. Inspection identifies longer apply preparation and
admission tails: candidate workers in original repeat 1 and follow-up repeats
1/3 spend 1.16–1.20 seconds in the existing directory boundary checks. The final
follow-up predecessor also reproduces that mechanism (1.15-second boundary,
1.44-second apply run, p99 4.64 seconds). The separate 2.43-second journal read
explains the original freshness miss. These profiles connect the tails to the
unchanged mechanisms tracked in [#91](https://github.com/supabricks/platform/issues/91)
and [#96](https://github.com/supabricks/platform/issues/96); they do not establish
statistical latency equivalence or prove why a particular fixture gets a larger
batch. The fresh comparisons and individual pairs remain available for the next
slice, with no timing improvement credited to SP00.

Median CPU and peak memory across these six pairs change by −0.64% and −1.81%.
The additional block does not erase the original freshness miss or establish a
reliable five-second product guarantee. Original and follow-up blocks are named
separately; only the original block is the mandatory four-cell protocol.

## Host, evidence and validation

The mandatory run took 54.28 minutes, including its initial quiet wait. All twelve
pairs were accepted on their first attempt. The shortest pre-trial quiet receipt
was 304.98 seconds. All 24 teardown receipts have zero remaining/leaked descendants.
The 649 host samples have a maximum gap of 5.05 seconds; the only build-activity
sample was initial discovery before the quiet wait. Frequency sensors covered all
16 logical CPUs; no temperature sensors were available. The host remains shared:
unknown or sub-sample-duration builds can escape the detector.

Thirty-one accounting tests pass in the qualification container, including missing
markers, false success on failure, changed workload/identity, malformed profiles,
cleanup mismatch, duplicate/missing pairs, resume, bounded host output, monitor
failure and archive round-trip/tamper rejection. The frozen implementation commit
passed all ten CI checks. The added CI workflow runs these accounting tests on
future relevant changes. [Issue #94](https://github.com/supabricks/platform/issues/94)
records the repaired predecessor resume-validation gap.

The original 45-second profiler remains bounded at 8 MiB/worker and accumulates
collector samples in memory. SP00 documents a chunked cumulative-counter design
for sustained runs; it does not claim hour-long profiling is implemented. Group,
checkpoint and retry capabilities remain explicit, and new probes require matched
activation controls before later optimizations can be credited.

## Contribution decision

**Keep — reliability/enabling.** The 24-trial mandatory comparison and six-trial
investigation are retained, and the unexpected freshness/tail results have
identified existing journal and directory-scan contributors. All 30 teardown
receipts are clean. No runtime speedup or latency equivalence is claimed.

| Slice | Changed mechanism | Measured contribution | Remaining constraint | Decision |
| --- | --- | --- | --- | --- |
| SP00 | Reproducible paired orchestration and accounting | Frozen identities, quiet-host receipts, explicit failure/input denominators, raw evidence and validated export; 30 retained trials | Overload fails 6/6 per arm; original low-load freshness miss remains; capture ~30 transactions/s and source input below 1,000 rows/s | Keep — reliability/enabling |

SP01 can now measure bounded journal-read recovery against this frozen baseline.
It must retain the freshness tail and distinguish reliability gains from throughput
or latency gains. SP02/SP03 address durable capture and reader/writer coexistence;
the directory-scan and source-capacity work remain separate measured slices.
