# EQ02: publication verification scheduling

Status: the correction for [#186](https://github.com/supabricks/platform/issues/186)
passes protocol/state-machine tests and matched daemon measurements. All 12 installed triggered/continuous/maintenance checks pass with zero leaked
descendants. Fresh full SF1 attempt 09 is running. SP remains frozen.
[Evidence](tpcds-evidence/2026-10-07-eq02/publication-verification/summary.json).

## Retained failure and attribution

SF1 attempt 08 exhausted its unchanged 7,200-second load/drain deadline:
12,746,017 of 19,557,335 rows committed, 12,680,481 observed published.
The loader waited 6,998.742 seconds for publication-window headroom. Capture
reported no error and remained current; the analytical view was lagging.
Cleanup observed 747 descendants and left zero. The external worker sampler
observed 619 workers, with a maximum recorded high-water mark of 757,727,232
bytes, below the unchanged 768 MiB worker limit. Sampling can miss short peaks.

The retained SQLite publication receipts contain 884 completed epochs. Their
summed request-to-publication intervals total 6,911.514 seconds; the interval
from worker descriptor preparation to publication totals 5,152.472 seconds
(74.55%). This is a wall-clock phase attribution, not a CPU profile. Delta merge
metrics total 466.008 seconds and twelve compactions total 353.558 seconds;
these are nested worker costs and must not be added to the request intervals.

The final completed 16,384-row inventory batch takes 2.825 seconds from worker
start to descriptor preparation, then 9.693 seconds to publication. Its file
inventory contains 160,858,085 bytes. The publisher hashes at most 4 MiB per call,
but previously received a call only on the daemon's general 200 ms maintenance
tick. Repeated scheduling gaps dominate verification as the inventory grows.

## Change and invariants

Commit `e945eae25c999eb48d6a5e5c7a3728ed24c29871` gives an active verifier a
20 ms continuation interval. General maintenance and idle publication retain
their 200 ms cadence. Each verification still yields after at most 4 MiB, allowing
IPC and other work between chunks. Worker receipts accepted during maintenance
remain eligible for publication in that same turn. Shutdown stops publication.

Every file is still hashed, including previously published immutable files.
Durability checks, source freshness, policy/head fencing, atomic publication,
corruption rejection and restart behavior are unchanged. No source, memory,
disk, transaction or publication-backlog limit is increased. No checksum cache,
new bootstrap path or standalone replication service is introduced.

## Matched measurements

The real-daemon protocol fixture writes a 128 MiB synthetic inventory, exercises
successful publication and rejection of a corrupt final byte, and sends status
requests while verification proceeds. These files are protocol fixtures, not
Parquet or a PostgreSQL/Delta throughput benchmark. Bare copies of the two
verified package binaries run on CPUs 0–7; no build/test workload competes with
the measurements. Full installation startup is deliberately outside this fixture.

| Three paired runs | Predecessor | Candidate |
| --- | ---: | ---: |
| Successful publication median | 7.018 s | 0.877 s |
| Successful publication range | 7.015–7.023 s | 0.875–0.892 s |
| Tail-corruption rejection | All pass | All pass |
| Maximum observed status-request time | 53.3 ms | 56.8 ms |

The measured publication improvement is **8.00×** for this fixture. It does not
establish an eightfold improvement in complete sync throughput. An additional
paired run with corrected process sampling records approximately 15.5–15.7 MiB
peak daemon RSS. Its CPU totals also include status handling; the slower run
receives many more probes and is not a matched-CPU-cost comparison.

The first diagnostic used installed binaries directly and hit the intended
installed-root guard when the bare test Store reopened the result. Its first
publication timing is retained but excluded. The three paired timing runs have
no resource samples because their sampler filtered the renamed process name;
the additional pair uses executable identity. All failed/incomplete diagnostic
attempts remain explicit in the evidence summary.

Validation: 13 analytics protocol tests, one daemon ownership/restart test and
53 sync state-machine tests pass, including existing subprocess publication
crash/replay tests. The new daemon fixture checks IPC progress and late checksum
failure. The verified unsigned runtime changes only the native binary relative
to attempt 08; its Python workers and reviewed Delta build are identical.

## Remaining qualification

Installed triggered (3), continuous (4), and maintenance (5) checks pass,
including the existing 50-row/s freshness gate, pinned readers and crash/restart
recovery. Attempt 09 is retrying all 24 SF1 tables with continuous sync healthy before the first COPY and the original
8-CPU/16-GiB, 65,536-unpublished-row and two-hour bounds. A passing load must be
followed by exact PostgreSQL/Delta table comparison and all 103 analytical SQL
statements compared against the retained independent Spark reference.

The preceding exact-archive CI run built both Delta runtimes and passed both
platform sync suites. Separate Linux catalog crash-readiness (#187) and project
notebook process-loss (#188) failures still block an overall green release run.
No complete SF1, SQL, release or SP qualification is claimed here.
