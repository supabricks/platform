# SY07: sync recovery and storage maintenance

[Plan](../plans/analytical-sync-implementation.md) · [Delivery ledger](../plans/status.md) · [SY06](sy06-sync-surfaces.md)

Status: implemented; local Linux source qualification passes, cross-platform CI pending. Exact installed
archive qualification remains SY08. PostgreSQL → analytics remains the supported
direction; these changes do not introduce a second writable copy or live DDL
migration.

## Published spool retention

The daemon sends its durable published cursor to the capture worker. Only that
cursor authorizes reclamation. Once at least 256 transactions or 1 MiB can be
reclaimed, the worker removes a prefix in one SQLite transaction, capped at 256
transactions and 16 MiB per pass. The latest transaction at or below the cursor
remains as a checksum anchor for reconnect replay. Unpublished transactions are
never removed to make space.

Deletion atomically persists a checksummed summary bound to the capture identity:
the predecessor LSN, most recent barrier and last data LSN. Opening the spool
verifies the remaining chain from that summary. An applier whose cursor precedes
the reclaimed prefix fails with `source_history_lost`. Corrupt summaries, changed
replay payloads and regressed published cursors fail closed. Capture acknowledgment
still follows durable append; reclamation cannot advance it.

SQLite readers retain their transaction snapshot. If a reader prevents a pruning
commit, pruning rolls back and retries later. New spools use incremental
auto-vacuum; existing spools reuse free pages without a full-size `VACUUM` copy.
Barrier timestamps survive reclamation, including spools created before SY07.
SQLite-full append failures preserve the previous durable cursor.

## Compaction and reader retention

An admitted apply allocates a new storage generation when the previous descriptor
has a table at version 64, at least 2,048 inventory files, or at least 512 MiB of
generation data. The generation UUID is part of the durable run admission, so
retries cannot allocate different destinations.

Compaction streams each table's **selected version** into version zero in a new
root, preserving Arrow types, nullability, decimal precision and row counts.
It records the source descriptor hash, selected files, output files and byte/row
metrics. Files and directories are fsynced before an atomic directory rename.
Only then does the ordinary bounded apply execute. The existing SQLite publisher
atomically exposes the complete new group map and cursor. A crash can leave an
unpublished destination, but cannot change the old published map.

Version-2 descriptors may now include `manifest.storage_generation`. Legacy
descriptors use the capture UUID. Capture identity still binds source lineage and
authority; storage identity binds the immutable generation selection. Sail and
the isolated catalog epoch-view builder both honor this selection. Catalog views
continue to exclude historical, future and orphan rows.

Compaction never vacuums the previous root. Historical snapshots, SQL/notebook
sessions, explicit leases, catalog publications/bindings and an active applier's
source/destination retain their roots. Existing explicit snapshot collection
releases unpinned history; the controller removes an old root only after every
reference drains and its ownership marker matches the durable registry. This can
now happen while the source capture remains live. A stopped backup includes the
referenced files and registry in its independently verified copy.

## Resource envelope

| Resource | Bound or behavior |
| --- | --- |
| Individual storage generation | Existing 1 GiB / 4,096 files / fewer than 1,024 versions |
| Retained incremental roots per installation | 4 GiB, 32,768 files, 256 directories, including initialization roots |
| Compaction input | Selected active files; conservative rewrite reservation before writing |
| Arrow stream | 256-row batches, one batch/fragment readahead, 32 MiB decoded batch ceiling |
| Output | Uncompressed Parquet, 1,024-row groups, 16 MiB target file size |
| Worker | Existing single apply worker, 768 MiB sampled RSS, five-minute deadline, three attempts |
| Disk | Existing 128 MiB free reserve plus write reservation |
| Retained run journal / retry receipts | Existing 1,024 / 4,096 installation limits; no silent expiration of idempotency keys |

Pinned history can prevent further admission. Release unneeded pins and explicitly
collect history before retrying; never delete a generation directory manually.
Failure after an apply starts retains the last published epoch and follows the
existing explicit-resync contract. Compaction is periodic, not a full rewrite per
small batch. It still incurs full selected-row I/O and is not a lag guarantee.
Manifest metrics report compaction input/output files, bytes, rows and duration,
alongside existing per-apply amplification and total retained bytes. Retention
includes logical file sizes conservatively, even for shared inodes.

## Upgrade, restore and source retirement

Catalog schema **29** registers roots and gates older controllers before pruning
or compaction writes occur. Schema 28 upgrades through the existing verified,
stopped-backup migration; supported earlier schemas retain their migration path.
The bundled Python package includes the maintenance worker. Snapshot format 1 and
legacy format-2 descriptors remain readable; downgrading a schema-29 root is
rejected. Roll back by restoring the pre-upgrade backup with its original release.

Restoring a stopped backup preserves compacted epochs and pruned spool bytes but
fences copied capture intent and cancels unfinished apply work. Resume requires
explicit source review and resync. Source restore/fork, WAL loss, schema drift,
credential/authority changes and worker identity mismatches retain the existing
fail-closed behavior. Supported schema changes use resync; schema replication is
not automatic. Deleting a capture retires its owned slot/publication/spool while
retained analytical epochs remain readable.

## Qualification

| Boundary | Evidence |
| --- | --- |
| Spool prune commit/vacuum, checksum anchor, corruption, held reader, actual SQLite-full error, page reuse, barrier recovery | `python/analytics/test_maintenance.py` |
| Compaction table write/rename/apply crash, exact old/new rows, disk and retention pressure, changed source receipt | Same worker suite; independent Delta readers |
| Durable rollover admission, retry identity, active writer pins, explicit lease/GC, catalog row isolation | Rust store and epoch-view tests |
| Stopped schema-28 upgrade and interruption recovery, older backup compatibility | Rust installed-process recovery suite |
| 64-version automatic rollover, crash replay, pinned Sail reader, 300-transaction burst pruning, restart, live-source GC, backup/restore, retirement | `e2e/native/sync_maintenance.py`, Linux/macOS source CI |
| WAL loss, source/schema/credential fences and corruption | Existing `e2e/native/capture.py` and `continuous.py` regression gates |
| Revocation, cross-project denial, service credentials and catalog pins | Existing SY06 store, native sync-surfaces and governed/catalog regression gates |

These are bounded engineering checks. They do not establish an unlimited lifetime
for run journals or a replication-lag SLO. Exact release archives, offline startup
and the full supported local/governed profile matrix remain the SY08 release gate.

Local Linux evidence (2026-09-23): portable core/local Rust suite including all 30
recovery tests; 43 analytical worker tests; 56 native packaging tests; all five
real PG/Sail maintenance checks, including stopped backup/restore. Source CI also
runs the native maintenance gate on Linux x86_64 and macOS arm64. These results
do not qualify a newly assembled release archive.
