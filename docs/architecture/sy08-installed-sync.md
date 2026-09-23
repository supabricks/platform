# SY08: installed synchronization qualification

[Plan](../plans/analytical-sync-implementation.md) · [Delivery ledger](../plans/status.md) · [SY07](sy07-sync-hardening.md)

Status: qualification harness implemented; exact candidate acceptance is pending.
A passing source test or a reused alpha.35 version label does not qualify a new
archive. Acceptance requires the combined `sy08-evidence` artifact for that
candidate's Linux x86_64 and macOS arm64 archives and the Linux governed profile.

## Installed boundaries

`install/native/qualify_sync.py` stages a signed engineering installer, installs
through curl into a path containing spaces, and runs the installed binary in
place. It verifies the complete installation before and after the suites and
records the archive, release inventory, binary and bundled worker hashes.
`e2e/native/installed_sync.py` uses the bundled Python, capture/apply workers,
Sail and Unity Catalog defaults. It refuses source-runtime substitutions. Every
fixture has a private data directory; the process gate accounts for descendants
and rejects leaks, failed exit codes and timeouts.

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
passed triggered and governed surfaces. Archive SHA-256 is
`28b2a581f3caf4946c514dcf1a91a6f0cfe3f5872fa1c4d6e782218b0f3ca79e`;
release identity is
`1910a30331943685e81935084ba1f3e6411ca8bd89bc677fbee2e487d19ee8b9`. An initial continuous workload sustained
50.06 changed rows/second but measured 5,198 ms p95 publication lag and failed the
5,000 ms gate. Its burst lag was 3,255 ms. This result does not qualify continuous
performance. A signed-installer smoke run passed triggered checks with zero leaked
processes but failed continuous at 18,744 ms p95 under increased host load; it
also cleaned up all descendants. Neither run establishes offline acceptance.
The candidate's own offline Linux/macOS and governed matrix must
complete before this status can change to qualified.
