# SY08: installed synchronization qualification

[Plan](../plans/analytical-sync-implementation.md) · [Delivery ledger](../plans/status.md) · [SY07](sy07-sync-hardening.md)

Status: **qualified** on 2026-09-23 for the exact `v0.1.0-alpha.36` archives below
(catalog 29). [PR #86](https://github.com/supabricks/platform/pull/86) contains the
implementation and acceptance record; merge remains separate.

**Follow-up gate failure:** a fresh build at documentation commit `7117b36` failed
Linux continuous latency and Linux environment-console qualification in
[run 35916809053, attempt 1](https://github.com/supabricks/platform/actions/runs/35916809053/attempts/1).
The historical archive acceptance below remains exact; it does not sign off the
current PR. See the [follow-up investigation](#follow-up-linux-investigation).

[Run 35908989254](https://github.com/supabricks/platform/actions/runs/35908989254)
passed all 33 jobs, including the combined R04/SY08 collector. Both local-owner
targets passed all 26 installed sync checks. The dedicated Linux governed profile
passed identity (11), data (17), sync (14), upgrade (4), and TLS browser (14) checks.
All observed descendants were cleaned up. Required CI and both native source
suites also passed on implementation commit `57b111de5b5d2845b54231c91c2443684afc2bd8`.
The archives contain tested merge `8ea59d8c5c70f798165c1f1c3b981a2f51d666d0`.

| Target | Archive SHA-256 | Release inventory identity |
| --- | --- | --- |
| linux-x86_64 | `a3976187f4892a78404bfda4ad7370ab3f6001d57c1dd979a65fe0a6920d7c72` | `a81619398391c900be51a7441d61e78c5854d3258e66c46d5d5a62cbd534398b` |
| macos-arm64 | `fb1240fa589088afeaee579ad1783db854d65e74c9e61a8f2d36fc79507aa6c8` | `473eb0868599fc7552076ad003419e18deaec7b9dbc0858632dc70344993077a` |

Retained evidence: [SY08 acceptance](sy08-evidence/sy08-evidence.json),
[inherited R04 acceptance](sy08-evidence/r04-evidence.json),
[R04 summary](sy08-evidence/r04-evidence.md),
[Linux governed ingress receipt](sy08-evidence/r04-evidence.governed.json), and
[stage timings and cleanup](sy08-evidence/workload-stages.json).
The SY08 report and ingress receipt both hash the retained R04 report. These
identities qualify the recorded archives; a new build needs its own acceptance.
This documentation-only follow-up records the tested candidate without changing
its packaged inputs. Alpha.35 remains the qualified historical predecessor.

| Target | Logical CPUs / RAM | Lag p50 / p95 / p99 (ms) | Burst (ms) | Changed rows/s | OLTP p95 baseline → during (ms) | Parquet/input bytes |
| --- | --- | --- | --- | --- | --- | --- |
| linux-x86_64 | 4 / 15.61 GiB | 1,790 / 2,394 / 2,527 | 3,515 | 50.06 | 3.350 → 5.830 | 15.556× |
| macos-arm64 | 3 / 7.00 GiB | 2,240 / 3,461 / 4,206 | 3,055 | 49.97 | 3.123 → 9.524 | 12.880× |

The unchanged 50-changed-rows/second workload and 200-row burst both meet the
5,000-ms gates. These are commit-to-publication measurements; opening a new Sail
session and query execution are separate. The full reports retain resource usage,
write amplification, and the limits described below.

## Installed boundaries

`install/native/qualify_sync.py` stages a signed engineering installer, installs
through curl into a path containing spaces, and runs the installed binary in
place. It verifies the complete installation before and after the suites and
records the archive, release inventory, binary and bundled worker hashes.
`e2e/native/installed_sync.py` uses the bundled Python, capture/apply workers,
Sail and Unity Catalog defaults. It refuses source-runtime substitutions. Every
fixture has a private data directory; the process gate accounts for descendants
and rejects leaks, failed exit codes and timeouts. Installed resource sampling
and shutdown inspect owned processes directly, avoiding the macOS system `ps`
executable under Seatbelt. A producer/collector contract test verifies all four
suites emit the required check-name format.

The `release-sync` jobs in `native-release.yml` run after native assembly:

| Profile | Isolation and execution |
| --- | --- |
| Linux x86_64 local owner | Unprivileged fixture in a loopback-only network namespace; system-only PATH |
| macOS arm64 local owner | Seatbelt denies external networking, Homebrew and host Java; system-only PATH |
| Linux governed shared server | Additional sync suite inside the existing offline, dedicated 4-core/16-GiB UC09.8 qualifier; unchanged identity, data, upgrade and TLS browser gates |

The local-owner jobs also exercise service-bound sync with the real catalog.
That does not replace the dedicated Linux governed job. macOS governed isolation
remains unsupported. Dependency installation happens before network isolation;
product workers and dependencies come from the archive during qualification.

After notebook/dependency assembly and cleanup, the builder precompiles the
incremental worker's imported Python modules with
checked source hashes and relative code filenames. This avoids repeated source
compilation at every bounded apply, remains valid after relocation, and refuses
to use stale bytecode when source changes. Runtime bytecode writes remain disabled;
compiled files are covered by the ordinary release inventory. The import closure
adds approximately 14 MB uncompressed in the local Linux experiment; other Python
packages remain source-only. `provenance/analytical-build.json` records the count,
size, invalidation mode and individual cache hashes. A final-inventory check
rejects caches removed or changed by a later packaging stage.

The daemon consumes accepted worker receipts in the same turn, and incremental
publication commits immediately after bounded verification reaches its durable
ready state. Previously these handoffs each waited for another timer turn. The
4-MiB-per-turn checksum budget, ready record, durability, source/policy/head fencing,
and atomic group/cursor transaction are unchanged. Crash recovery still resumes
from the persisted ready state.

Both the worker and publisher avoid repeatedly flushing immutable files already
covered by a published inventory in the same storage generation. Every byte is
still checksum-verified. New files, replayed unpublished files, directory entries,
and compacted generations retain their flushes. Matching a path in a different
generation is never proof of durability. Workload reports also include timings
from admission to worker start, preparation, and publication for diagnosis.

## Checks and measurements

| Suite | Installed checks |
| --- | --- |
| Triggered (3) | Fixed barrier with an open transaction, idempotency, pinned reader and idle version stability; more than the production 16-MiB apply input budget across complete transactions; restart from the same checkpoint and retirement |
| Continuous (4) | Sustained workload and burst with atomic two-table results; idle stability and pinned reader; pause/resume, capture SIGKILL and daemon restart; schema fencing and explicit resync |
| Maintenance (5) | Automatic 64-version rollover and pinned Sail reader; published-prefix reclamation and restart; collection after pins drain; stopped backup/restore; source retirement retaining analytical data |
| Governed surfaces (14) | Separate source/manage/result authority; service lifecycle and revocation; unsupported-source denial; immutable catalog epoch views; cross-project binding and notebook restart; backup reconciliation and identifier-only audit |

Deterministic compaction failpoints, full-disk injection and corrupt-spool tests
remain in the SY02–SY07 source gates. The installed maintenance suite reaches the
production threshold using ordinary writes and keeps the installed workers
unchanged. It does not claim a physical power-loss trial.

The continuous fixture uses two tables of 10,000 rows, updates both tables in
750 transactions over 30 seconds (50 changed rows/second), then commits a
200-row burst. Admission uses the product defaults: desired freshness 5 seconds,
minimum batch interval 500 ms. Passing requires sustained throughput of at least
45 changed rows/second, workload duration at most 33 seconds, sustained p95
commit-to-publication lag at most 5,000 ms, and burst lag at most 5,000 ms.
Thresholds are not widened for a slow candidate.

Each source transaction's XID is correlated with its complete captured end LSN
and the first durable publication covering it. The observer records markers
before normal spool reclamation removes them; it does not disable pruning or
hold a SQLite read transaction throughout the workload. Lag begins at the
client's COMMIT acknowledgement; it excludes opening a later analytical session.

Reports retain p50/p95/p99 publication lag and OLTP transaction duration, OLTP
baseline and p95 ratio, achieved throughput, hardware capacity, sampled owned
RSS, allocated data disk, retained WAL, spool usage and sampled CPU time. Storage
measurements report captured input bytes, new Parquet bytes, their ratio,
inventory/file-generation peaks and compaction output bytes. Parquet amplification
excludes Delta log metadata and reports compaction separately. Resource sampling
can miss brief peaks; RSS can double-count shared pages and CPU is a sampled lower
bound. Installation and qualification-client disk are excluded. These are bounded
engineering measurements, not an unlimited-workload latency guarantee. SY07's
finite retained-root and run/idempotency-journal limits still apply.

## Acceptance and reproduction

The workflow retains `release-sync-linux-x86_64/sync.json`,
`release-sync-macos-arm64/sync.json` and the existing Linux governed report.
`install/native/sync_evidence.py` accepts them only after the complete inherited
R04 collector succeeds at the same reviewed revision. It rejects missing checks,
source overrides, mixed identities, changed worker hashes, missing offline
isolation, cleanup leaks, invalid numbers and missed workload thresholds. The
result stores only fixed measurements and report hashes, excluding arbitrary
fixture payloads, SQL and credentials. The `sy08-evidence` artifact also hashes
its inherited R04 report.

For a developer smoke run against an already extracted candidate:

```bash
python3 -m venv /tmp/sync-clients
/tmp/sync-clients/bin/pip install -r e2e/native/requirements.txt -r e2e/native/catalog/requirements.txt
/tmp/sync-clients/bin/python e2e/native/installed_sync.py \
  --release /absolute/path/to/supabricks --suite continuous \
  --report /tmp/installed-continuous.json
```

Available suites are `triggered`, `continuous`, `maintenance` and `governed`.
A developer smoke run does not establish offline or cross-platform acceptance.
Use the reviewed workflow for the complete signed-install and isolated matrix.
Install and operate a qualified candidate using [managed sync](../handbook/managed-sync.md),
[triggered sync](../handbook/triggered-sync.md) and
[continuous sync](../handbook/continuous-sync.md). Keep the archive and its exact
qualification identity together when distributing engineering builds.

## Earlier candidates and diagnostic runs

Local Linux smoke checks against the unchanged SY07 archive from
[run 35829067775](https://github.com/supabricks/platform/actions/runs/35829067775)
passed all 3 triggered, 14 governed-surface and 5 maintenance checks. Maintenance
observed 432 descendants with zero leaks. Archive SHA-256 is
`28b2a581f3caf4946c514dcf1a91a6f0cfe3f5872fa1c4d6e782218b0f3ca79e`;
release identity is
`1910a30331943685e81935084ba1f3e6411ca8bd89bc677fbee2e487d19ee8b9`. An initial continuous workload sustained
50.06 changed rows/second but measured 5,198 ms p95 publication lag and failed the
5,000 ms gate. Its burst lag was 3,255 ms. This result does not qualify continuous
performance. A signed-installer smoke run passed triggered checks with zero leaked
processes but failed continuous at 18,744 ms p95 under increased host load; it
also cleaned up all descendants. Neither run establishes offline acceptance.
A separate development copy with build-time checked-hash bytecode then passed
all four continuous checks, including recovery and explicit resync: 50.05 changed
rows/second, 4,377 ms p95, 5,046 ms p99, and 3,061 ms burst lag, with zero leaked
descendants. This experiment deliberately marks its provenance dirty and cannot
satisfy the archive collector. These development measurements did not qualify
the candidate.

Follow-up Linux smoke checks passed all 17 inherited governed data cases and all
14 sync governance cases inside a network-disabled, four-CPU/16-GiB container.
This checks the container path on the development host, not the dedicated CI host
profile. The qualifier now installs the locked WebSocket/catalog clients needed
by notebook sync checks. The updated process sampler passed continuous recovery
with 3,898 ms p95, 4,208 ms p99 and 3,359 ms burst lag, with no descendant leaks.
The predecessor's governed restore failure occurred twice in CI but did not
reproduce in these runs; retained diagnostics now include allowlisted native
error codes and PostgreSQL states without exporting messages or credentials.

A complete signed-installer smoke run of that development copy then passed all
26 installed sync checks inside the network-disabled four-CPU/16-GiB container.
The four suites observed 838 descendants with zero leaks, and both installation
inventory verifications passed. Continuous sync measured 50.06 changed
rows/second, 4,376 ms p95, 4,797 ms p99 and 3,287 ms burst lag. This also exercised
the actual string check names consumed by the evidence collector. The archive
still has deliberately dirty development provenance; these results validate the
harness and do not substitute for the alpha.36 CI archives.

The first alpha.36 CI archives in
[run 35890370041](https://github.com/supabricks/platform/actions/runs/35890370041)
passed triggered sync on both targets but failed continuous p95 at 5,961 ms
(Linux) and 5,943 ms (macOS); cleanup passed. Inspection found a packaging-order
bug: the build report claimed 514 compiled modules, but later notebook cleanup
removed every shared-runtime cache, leaving only eight worker caches. Compilation
now runs after all dependency assembly, and the final inventory must contain
every reported cache with its recorded hash and total size. Those first archives
remain unqualified. The corrected payload was subsequently tested in a fresh matrix.

A development copy of that exact alpha.36 Linux payload, finalized after notebook
cleanup and marked dirty, passed all four continuous checks offline with 514
retained caches: 50.06 changed rows/second, 4,544 ms p95, 4,835 ms p99 and
2,585 ms burst lag. Its 131 observed descendants exited without leaks. This
confirms the packaging fix locally; it is not a replacement archive qualification.

That first alpha.36 candidate passed the dedicated Linux governed CI job,
including all five identity/data/sync/upgrade/TLS-browser suites. The predecessor's
post-restore query failure did not recur. This is partial evidence only: the
corrected bytecode payload must repeat this gate with the rest of the matrix.

The corrected archives from
[run 35895998663, attempt 1](https://github.com/supabricks/platform/actions/runs/35895998663/attempts/1)
retain the compiled caches, but still failed the initial continuous workload
qualification. Linux measured 5,496 ms p95 and 3,474 ms burst lag; its OLTP p95
rose from a 3.652 ms baseline to 103.724 ms during sync. macOS measured 3,690 ms
p95 but missed the burst gate at 5,059 ms. Both sustained approximately 50 changed
rows/second and cleaned up without leaks. These results remain failures against
the unchanged limits; same-archive repetitions investigate variation and do not
turn the operating envelope into a latency guarantee.

The [failed-attempt measurements](sy08-evidence/attempt-1-failures.json) retain
archive identities, source/report hashes, checks, cleanup and timings independently
of GitHub's artifact handling during retries. The unchanged corrected Linux
archive also passed all four continuous checks in the local offline container:
4,749 ms p95, 5,225 ms p99, 3,524 ms burst lag and 50.05 changed rows/second,
with no leaks across 129 descendants. That comparison demonstrates host/run
variation; it does not explain its cause or replace the required CI pass.

On [attempt 2](https://github.com/supabricks/platform/actions/runs/35895998663/attempts/2),
Linux passed all 26 installed checks (3,185 ms p95 and 3,329 ms burst), but macOS
again failed (5,555 ms p95 and 5,438 ms burst). The
[attempt-2 results](sy08-evidence/attempt-2-results.json) retain both outcomes.
That candidate remains unqualified; a successful retry on one target
does not erase the earlier failures or qualify the other target.

An unmodified Linux archive profile in the offline 4-CPU/16-GiB container showed
601–852 ms between worker result creation and publication. Its workload measured
4,851 ms p95 and 4,004 ms burst. This identified avoidable daemon handoff waits
and motivated the same-turn publication change described above. These local
diagnostic runs do not replace fresh archive qualification on both targets.

The development binary with the handoff change passed all four continuous checks
in the same isolated container. Median receipt-to-publication wait fell from
730 ms to 305 ms (range 157–529 ms); sustained p95 was 4,729 ms, burst 3,927 ms,
and throughput 50.03 changed rows/second. Concurrent local compilation/testing
means the end-to-end timings are diagnostic, not a controlled benchmark. All
38 sync state-machine tests and 12 publication integration tests passed, including
SIGKILL at every publication boundary and a new check that same-turn publication
still yields when checksum verification exceeds its existing per-turn budget.
After local compilation/tests finished, a second development run passed all four
continuous checks at 3,446 ms p95, 3,609 ms p99, 3,018 ms burst and 50.04 changed
rows/second. Receipt-to-publication median was 274 ms (133–390 ms range). The
complete local-runtime test command passed 327 tests; the workspace command
stopped at the operator chart test because Helm was not installed locally.

The [handoff candidate](https://github.com/supabricks/platform/actions/runs/35904364715)
passed required CI, both complete native suites, and all 26 installed Linux sync
checks (2,608 ms p95, 3,167 ms burst). macOS passed triggered checks and the p95
limit at 4,813 ms, but its 6,141 ms burst remained a failure. The
[macOS receipt](sy08-evidence/handoff-macos-failure.json) retains that result.
This motivated avoiding repeated fsyncs of the growing published immutable
prefix. The flush optimization passed 21 Python incremental/maintenance tests,
39 sync state-machine tests, and 12 publication tests. These diagnostic runs
preceded the successful exact-archive qualification recorded above; no
performance gate was changed.
The optimized development copy passed all four continuous checks offline at
3,413 ms p95, 3,947 ms p99, 3,048 ms burst and 50.06 changed rows/second. Across
23 measured batches, worker-start-to-preparation p95 was 1,268 ms and
preparation-to-publication p95 was 429 ms. These Linux diagnostics do not establish
the macOS operating envelope.


## Follow-up Linux investigation

[Retained failures](sy08-evidence/followup-linux-failures.json) distinguish the
fresh build at `7117b36` from the previously accepted archives. Required CI
passed, but continuous p95 was **5,698.660 ms**, above the unchanged 5,000-ms gate.
The 200-row burst passed at 2,736.113 ms. There were no leaked descendants.

| Measurement (p95) | Accepted Linux archive run | Follow-up failing Linux run |
| --- | ---: | ---: |
| Source transaction before starting sync | 3.350 ms | 81.235 ms |
| Source transaction during sync | 5.830 ms | 123.738 ms |
| Apply admission → worker start | 20 ms | 228 ms |
| Worker start → prepared receipt | 883 ms | 1,821 ms |
| Prepared receipt → publication | 203 ms | 677 ms |

The source was already slow before capture started. Multiple pipeline stages
slowed down, and the serial fixed-cut batch pipeline makes a transaction arriving
after a cut wait for the preceding batch as well as its own. This is evidence of
host/runtime service-time variability, not evidence that every excess millisecond
is caused by a particular disk or CPU condition: that run did not retain host
pressure or per-transaction stage attribution. Do not relabel it a passing run
or exclude it from the gate.

A [local diagnostic](sy08-evidence/latency-investigation-local.json) with the
unchanged preceding runtime, four allocated CPUs and 16 GiB passed at 4,148.941 ms
p95. First-observed durable capture p95 was at most 634.689 ms after commit
(includes the observer's 500-ms polling interval); apply admission p95 was
2,361.368 ms after commit. Those measurements identify batch waiting, not only
logical capture, as a material contributor. Percentiles across stages are not
additive. The workload now retains per-transaction stage percentiles and Linux
host CPU/IO/memory pressure counters so future failures can be localized without
weakening the workload or threshold.

The console exited after two six-second health request deadlines while an
environment operation occupied the single writer. A deterministic source-mode
regression pauses the real daemon for 16 seconds: the old binary refuses the
original console connection; the fixed binary keeps its HTTP listener and the
same authenticated session recovers after resume. Project identity replacement
still terminates the listener. The health check uses the existing
binding/generation/instance-validated heartbeat, with a 120-second grace only for
transport timeouts. Explicit rejection, malformed replies and daemon loss still
fail closed; the change does not extend notebook leases, sessions, or permissions,
and does not replay user operations. The regression runs on Linux and macOS in
portable CI. Seventeen console unit tests also pass locally. The full Linux browser environment
suite passes all 16 checks, including offline export/import and two-project
isolation, in the [local receipt](sy08-evidence/console-health-local.json).

Fresh installed qualification of this runtime fix remains required. The private
local package used for diagnosis has a rebuilt binary and explicitly dirty
provenance; it is not represented as a qualified release archive.
