# EQ02 merge-readiness corrections

Head `50b0207` passes 46 CI checks but fails native-baseline analytics on both
platforms, Linux installed sync capacity, and Linux governed restore. Preserve
those failures separately from the [SF1 data and SQL results](eq02-sf1-results.md).
SP remains frozen. The next analytical slice is decimal AVG rounding (#200),
after CI and the #183 merge into its existing EQ01 base.

## Reviewed Delta wheel in every analytical test job (#201)

The baseline workflow still installs the upstream wheel while the production
compaction worker requires the reviewed bounded-write extension. Both targets
report `write_deltalake() got an unexpected keyword argument 'max_spill_size'`;
155 tests run with two failures and five errors. The aggregate check then fails.

Commit `9dee851` gives native-baseline the same locked source build, artifact
verification and explicit wheel installation already used by native-cell and
native-release. Both platform jobs must succeed for the stable analytical-probe
context to pass. No test is skipped and production bounded-write options remain.

## Retained memory evidence after a failed capacity phase (#202)

The Linux installed archive passes ten sync suites, then fails the 512-version
history RSS assertion. Its six descendants are cleaned up. The adapter used to
copy metrics only after all phases succeeded, losing the actual measured peak
when the assertion failed. Commit `f2c9fe1` retains each phase's measurements on
failure and records history high-water marks before/after append and after reads
of versions 0, 1 and 512. A regression checks that a failed child keeps its peak
and receives no passing checks.

An unchanged local engineering runtime passes a four-CPU repeat with history
peak 402,882,560 bytes. The exact Linux CI archive also passes in all three fresh local repeats: the first
repeat peaks at 383,909,888 bytes, rising from 256,249,856 after appends to
376,807,424 after the first retained reader. These repeats do not establish the
cause of the hosted failure. Keep #202 open until that allocation is attributed;
the 768 MiB threshold and all history/value assertions remain unchanged. Hosted
qualification must pass and now retains useful metrics if it fails again.

## New-generation readiness before governed SQL (#137)

The governed CI failure occurs 171 ms into the first positive SQL request after
restore and regrant. Authorization policy is unchanged and the saved branch
revision appears reconciled, but the operation is uncertain. Identity checks
pass; cleanup leaves zero of 223 observed descendants. The unchanged exact
archive passes all 17 governed checks locally, so the original denial is not
claimed to have been deterministically reproduced.

Code inspection finds a concrete admission gap: saved observed revisions survive
restart, while engine configuration and credential/role sanitation belong to the
current process generation. Commit `e37664f` checks native connection readiness
after authorization and before reserving a data operation or request key. A
not-ready reply admits no SQL and leaves the key unused. Status/find operations
remain available. The private runtime status reports process configuration, and
the restore fixture waits for that boundary before issuing its positive SQL.
It does not retry an admitted denial into success.

The regression proves that an unchanged observed revision is insufficient to
admit work until runtime preparation completes. All 224 local unit tests and the
daemon test pass, with four existing tests ignored; all 13 qualification-harness
tests pass. All three installed candidate runs pass all 17 governed assertions,
including revocation, scoped privileges, backup/restore credential rotation,
writer crash rollback, and bounded audit behavior.

[Evidence](tpcds-evidence/2026-10-07-eq02/ci-readiness/summary.json) binds the exact
CI archive, native-only candidate overlay, tests, failed reports, fresh repeats
and descendant cleanup. Current-head hosted CI remains required before merge;
local passes do not overwrite the failed CI receipts.

## Hosted follow-up and bounded Linux allocator pages (#202)

At head `81cbe52`, both analytical-baseline targets pass all 155 worker tests,
20 fixture tests, and synthetic qualification. The hosted governed release also
passes: 17 data checks plus identity, sync, upgrade, and browser suites. The
Linux installed sync job passes its first ten suites, then fails capacity again.
Its retained measurements now locate the growth before historical readers:

| History boundary | Hosted RSS high-water |
| --- | ---: |
| Before append | 164,040,704 B |
| After 511 append commits | 838,303,744 B |
| After the first historical read | 851,910,656 B |
| Original limit | 805,306,368 B (768 MiB) |

The exact failing archive passes locally at 386,416,640 B. A controlled allocator
probe (`_RJEM_MALLOC_CONF=thp:always`) raises its local append peak to
736,534,528–791,445,504 B. Linux `smaps_rollup` shows hundreds of MiB of anonymous
huge pages during those appends. The original hosted huge-page setting was not
captured; this reproduces the allocation pattern without claiming conclusive
attribution to that destroyed runner.

The candidate installed Linux analytical launcher sets jemalloc's `thp:never`
before Python starts. This fixes the allocator page policy independently of the
host or inherited allocator settings. It changes no Delta data, query, history,
compaction, file, or memory limit. macOS keeps its existing launcher policy.
Capacity telemetry now samples anonymous/huge-page bytes every 64 versions and
records the Linux kernel's huge-page policy.

Three fresh wrapper-only candidate runs, even when launched with inherited
`thp:always`, pass all three capacity assertions. Append high-water stays below
246 MiB, and complete history peaks are 390,426,624, 382,738,432, and 387,878,912 B.
History takes 7.110–7.138 seconds versus 7.052 seconds for the unchanged archive
under the local default policy. These short fixture timings do not establish a
general throughput result. All runs have zero leaked descendants. The full
hosted release qualification remains required before merging.

## Pinned input download interruption (#203)

The first macOS assembly attempt at `81cbe52` times out connecting to a pinned
notebook fixture wheel. Rerunning only failed release jobs reuses all successful
source artifacts and completes assembly; no runtime assertion is retried into
success. The download helper now permits at most three transport attempts,
writes a unique temporary file, checks the pinned digest before atomic cache
publication, and removes partial files on failure. Corrupt cached/downloaded
bytes, certificate verification failures, and permanent HTTP failures remain
hard failures. The URL, digest, and offline qualification requirements stay fixed.

All 82 native packaging tests and 13 qualification-harness tests pass, including
new connection/body interruption, retry exhaustion, checksum/certificate/HTTP
failure, valid-cache, and launcher environment tests.

[Follow-up evidence](tpcds-evidence/2026-10-07-eq02/ci-memory/summary.json)
retains the hosted failure, successful baseline/governed checks, transport
failure, exact archive identity, wrapper-only proof, six local runs, commands,
measurements, tests, and cleanup. SP remains frozen; SF1 is not reloaded.
