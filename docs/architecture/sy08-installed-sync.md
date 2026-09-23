# SY08: installed synchronization qualification

[Plan](../plans/analytical-sync-implementation.md) · [Delivery ledger](../plans/status.md) · [SY07](sy07-sync-hardening.md)

Status: qualification harness implemented; exact candidate acceptance is pending.
The candidate uses `v0.1.0-alpha.36` with catalog 29. Alpha.35 remains the last
qualified release. The new version avoids the installer’s intentional rejection
of different archive bytes under an already installed version. Historical
predecessor pins and upgrade gates remain unchanged.
A passing source test does not qualify a new archive. Acceptance requires the combined `sy08-evidence` artifact for that
candidate's Linux x86_64 and macOS arm64 archives and the Linux governed profile.

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
4-MiB-per-turn checksum budget, ready record, fsyncs, source/policy/head fencing,
and atomic group/cursor transaction are unchanged. Crash recovery still resumes
from the persisted ready state.

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

## Evidence still pending

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
satisfy the archive collector. The candidate's own offline Linux/macOS and governed
matrix must complete before this status can change to qualified.

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
remain unqualified; a fresh matrix is required for the corrected payload.

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
The candidate therefore remains unqualified; a successful retry on one target
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
