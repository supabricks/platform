# SY03: incremental analytical epochs

[Plan](../plans/analytical-sync-implementation.md) · [Delivery ledger](../plans/status.md) · [Workflow](../handbook/incremental-epochs.md)

Status: [merged in #81](https://github.com/supabricks/platform/pull/81), 2026-09-22. SY03 adds explicit local-owner row
application and publication on SY02's durable capture. It does not enable triggered
or continuous policies itself; managed triggered runs are added by
[SY04](sy04-triggered-sync.md), and continuous mode remains SY05. Governed source admission and
versioned-root Unity Catalog publication remain closed pending SY06 qualification.

## Boundary and row application

`sync apply CAPTURE_ID --key KEY` admits one bounded run. Its first publication
uses the verified frozen bootstrap at **F**. Later runs sample a captured target
and consume a contiguous prefix of complete durable transactions after the last
published end LSN. A run may stop before its target at the input budget; inspect
`applied_lsn` and submit another explicit run. Source WAL end, XID and individual
row LSNs never become publication cursors. An idempotency key identifies one
intent and returns its original admission receipt; `sync applied RUN_ID` reads
current state. A no-change run can publish an identical map at the same boundary.

The admitted schema remains SY02's single integer primary key and bounded
integer/text/varchar/decimal profile. Application preserves transaction order,
handles inserts, updates, deletes and primary-key moves, and resolves unchanged
TOAST values from the previous selected Delta version. Decimal conversion never
passes through floating point or context rounding. Duplicate/missing keys, unknown
messages, schema changes and journal gaps fail closed. Source reads after
bootstrap are logical capture and catalog checks; baseline rows come from the
isolated frozen child. Affected-key lookup and Delta merge read analytical storage.

Before writing any table, the worker fsyncs a typed plan containing the admitted
identity, input boundaries, previous epoch and final affected-key rows. Each changed
table gets one Delta merge with a deletion marker; unchanged tables retain their
version. Commit metadata binds the table write to the run and plan hash. Recovery
accepts exactly the previous version or its immediate successor with the matching
marker. It never reapplies against arbitrary latest state. Files and directory
entries are fsynced before the worker's ready receipt.

## Version maps and atomic publication

Data lives in `analytics/incremental/<capture UUID>/tables/<OID>`. Initialization
hardlinks immutable files from the private A01 baseline; future writes append new
Delta log/data names. Existing A02 version-1 generations are not edited. Automatic
Delta checkpoints/log cleanup are disabled, and SY03 never runs vacuum.

Each immutable version-2 descriptor lives in
`analytics/generations/<apply UUID>/snapshot.json` with its manifest. It names the
capture root and a complete table-to-version map. Its inventory pins all logs
through each selected version and conservatively retains existing Parquet files.
Later unpublished log files are allowed in the shared root but are never selected
by a reader. V1 retains its existing exact immutable-directory verifier.

The Rust publisher streams file checksums, fsyncs and promotes the epoch metadata,
then commits the epoch, all table mappings, branch head, incremental head, applied
cursor and successful run receipt in **one SQLite transaction**. Directory presence
and a worker receipt alone are not publication authority. A crash between table
commits leaves the previous map visible; a crash around the SQLite commit leaves
either the complete old map/cursor or the complete new map/cursor. Local Sail
sessions use explicit `VERSION AS OF` for every table and keep their selected epoch
until closed. Existing snapshot-backed local notebook inputs use that session path.

A full snapshot published by another workflow changes the branch head. Further
application from that capture is refused; explicit new enrollment is required.
Cancellation or abandonment after partial writes fences the generation as
`resync_required`. Restart reconciliation retries the same run up to three times.
Stale capture observations, source lineage/revision changes and policy fences
cannot authorize publication. Backup restore cancels copied apply intent and
requires capture re-enrollment.

## Retention and limits

Snapshot/session leases and existing catalog references retain epoch metadata.
An active apply also retains its previous epoch. The shared root stays while its
capture is not deleted or any epoch/pending run still references it. After explicit
capture deletion and all references drain, the controller removes only a root
whose ownership marker matches its recorded capture identity. The same rule covers
interrupted initialization directories. It does not sweep unknown directories.

| Resource | SY03 bound |
| --- | --- |
| Active incremental workers | One per installation |
| Complete journal input per run | 16 MiB; each transaction remains at most 4 MiB |
| Row operations / decoded values | 16,384 / 32 MiB |
| Persisted apply plan / epoch receipt | 64 MiB / 2 MiB |
| Shared generation | 1 GiB, 4,096 files, fewer than 1,024 versions per table |
| Disk write admission | 128 MiB free reserve plus conservative table rewrite reservation |
| Worker | 768 MiB sampled RSS, five-minute absolute deadline, three attempts |
| Delta spill / temporary storage | 64 MiB / 128 MiB |
| Durable run history / retry receipts | 1,024 / 4,096 per installation |

These are engineering limits, not a throughput or replication-lag SLA. Capture
spool pruning and per-file Delta compaction/vacuum are not implemented. Whole-root
retention is deliberately conservative; a long-lived generation eventually needs
explicit deletion and a new baseline when its finite budget is exhausted. SY07
owns maintenance and broader recovery qualification. Merge may scan unaffected
files and rewrite whole affected files; it is not byte-level row storage. Manifests
report input bytes, generation bytes, Delta merge metrics, new Parquet bytes per
commit and retained Parquet bytes, including reconciled commits. Each run still
re-verifies previous files and hashes its publication inventory; checksum read I/O
grows with retained storage. A small input batch is not a constant-cost operation.

## Evidence and remaining gates

The Python suite checks exact decimals, Unicode, unchanged values, key moves,
deletes, net-zero transactions, duplicate/missing-key rejection, corruption,
pre-write disk budgets and partial-commit reconciliation. Independent Python
processes read old/new Delta versions. A one-key update in an 8,001-row, nine-file
table removes only one active file and preserves every original immutable file.

Rust subprocess tests send SIGKILL after files-complete, after rename, before
SQLite commit and after commit. Every recovery preserves the complete group map
and matching cursor, retains the old epoch, and finishes exactly once. Additional
tests cover request scope/retries, cancellation, stale observations, restore intent
and rollback when table-map insertion fails. Catalog schema 25 migrates publication
identities from exports to a common artifact registry using the stopped/backed-up
upgrade path and a final foreign-key check.

`e2e/native/incremental.py` runs native PostgreSQL, the daemon, Delta and Sail. It
checks real source updates and external TOAST, pinned readers, a worker killed
between two table commits, daemon/compute restart, unchanged A02 bytes, and cleanup
after capture deletion and the final reader reference drains. Linux source tests
are recorded with the PR. All six required GitHub checks and both Linux/macOS
native-cell suites passed at `23c995f`, merged as `fe46d72`. The stopped
installed-upgrade regression also accepts the additive `incremental_snapshot=2`
format declaration and rejects unknown versions. A complete
new release archive and physical power-loss durability are not qualified by these
source tests.

Implementation uses the locked delta-rs runtime's [merge API](https://delta-io.github.io/delta-rs/usage/merging-tables/)
and [commit/post-commit controls](https://delta-io.github.io/delta-rs/api/transaction/).
