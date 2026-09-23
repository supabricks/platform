# SY02: durable PostgreSQL capture

[Plan](../plans/analytical-sync-implementation.md) · [Delivery ledger](../plans/status.md) · [SY00 decisions](sy00-capture-probe.md)

Status: merged in [platform #80](https://github.com/supabricks/platform/pull/80), 2026-09-22. This is an explicit local-owner capture
foundation. It does not apply rows to Delta, publish incremental epochs, or enable
triggered/continuous product modes. SY03 owns application/publication; SY04 and
SY05 own the corresponding product policies.

Maintenance update: [SY07](sy07-sync-hardening.md) adds published-spool pruning,
periodic storage generations and reference-aware reclamation. Limits and missing
maintenance described below record the original slice boundary.

## Contract and identity

A generation attaches to one active, manual-only SY01 snapshot policy at an exact
revision. Installation, deployment, project, branch, tenant, timeline, database,
policy revision, generation UUID and decoder version identify its private journal.
The qualified source is the shipped PG17/Neon native cell. Governed sources are
refused. There is one admitted generation per installation, including failed or
paused generations, until explicit deletion finishes. Starting capture requires
an installed analytical worker and a running native source.

The source profile is ordinary logged `public` tables with one non-null integer
primary key, integer/text/varchar/bounded-decimal columns, default replica identity
and default collation. RLS, partitions, inheritance, foreign/materialized tables,
generated columns and unsupported types are rejected as a whole group. The
existing engine control tables are excluded only after owner/type checks; their
OIDs are recorded. Limits are 128 tables, 128 columns/table and 128 KiB of schema
metadata. Views/sequences are not replicated. Type admission is not a claim that
SY02 has implemented value conversion or incremental row application.

`sbcap_<generation UUID without hyphens>` names the slot, publication and private
DDL-fence schema. Transactional ownership comments bind resources to the full
identity. The exact publication membership/options and schema are checked again
on reconnect and during capture. Unmanaged replication slots are refused, except
Neon's verified `wal_proposer_slot` physical-slot profile. Cleanup checks the source
identity and ownership markers before dropping resources.

## Durable boundary and bootstrap

1. Create the exact-table publication and transactional DDL fence, then create a
   `pgoutput` logical slot at consistent point **S**.
2. Persist S and the immutable schema in the private spool. Start streaming with
   pgoutput v1, messages enabled, and no streamed/prepared transactions.
3. Admit a private A01 hidden frozen export at **F**, requiring S ≤ F. All baseline
   row scans occur on the isolated child. The live-source monitor reads catalogs
   and replication status only.
4. Verify the A01 manifest's source identity, database OID, schema, boundary,
   complete file inventory and checksums. Hashing yields between 1 MiB chunks so
   capture and retention monitoring continue. Reverify on worker restart.
5. Keep only complete transactions in commit-end-LSN order. A future SY03 consumer
   combines the baseline with committed transactions whose **end LSN E > F**.
   XID, row LSN, server WAL end and snapshot transaction IDs are not apply cursors.

The private bootstrap cannot be published or discarded through A02. The guard
also covers the crash between export admission and capture-record linking. Delete
cancels an unfinished bootstrap or queues a completed one for existing analytical
GC. Existing immutable A02 snapshots and readers are unaffected.

The worker uses a bounded native Unix-socket replication reader rather than
materializing arbitrary libpq replication frames. An oversized wire length is
rejected before payload allocation. A SQLite journal stores normalized raw frames,
commit/end LSNs, the previous durable end and SHA-256. Repeated Relation frames are
validated against immutable metadata and omitted from transaction hashes so
reconnect replay is stable. The decoder has no acknowledgment authority.

SQLite uses `synchronous=FULL`, rollback journals and one exclusive writer. The
transaction row and captured cursor commit atomically. Parent directory entries
are fsynced before feedback is possible. Feedback write/flush positions are read
from that committed cursor; the apply position remains zero. A duplicate end LSN
must match the prior checksum. Startup verifies the complete chain, checksums,
source identity and that the source has not acknowledged beyond the spool.
Unknown messages, schema changes, missing history and corruption fail closed.

## Budgets and lifecycle

| Resource | Bound/behavior |
| --- | --- |
| Wire message / complete transaction | 1 MiB / 4 MiB, checked before acknowledgment |
| Spool | 16–512 MiB; default 512 MiB, SQLite page cap plus journal/write reservations |
| Free space | Reserve 64 MiB before transaction writes; disk errors never authorize feedback |
| Source retention | 32–512 MiB; default 512 MiB; monitor fences at 80% |
| Source decoding | 1 MiB logical decoding work memory and 64 MiB temporary-file limit per capture connection |
| Bootstrap | Separate existing A01 policy output/deadline limits |
| Admission | One live/paused/failed generation per installation; 128 generation records and 10,000 retry receipts |

Source `max_slot_wal_keep_size` is set and verified before slot creation, persisted
through `ALTER SYSTEM`, and supplied by the native compute configuration on
restart. PostgreSQL enforces it at checkpoints; it is **not an exact physical WAL
byte ceiling**. Monitor overshoot and checkpoint cadence still matter. The cap
also remains on that compute after capture deletion; deletion removes the owned
slot/publication/fence, not the conservative source setting. No pruning or spool
compaction is implemented before SY03's consumer/retention contract. A long-lived
capture will eventually require resync when its configured budget is exhausted.
See PostgreSQL's [replication settings](https://www.postgresql.org/docs/17/runtime-config-replication.html)
and [replication protocol](https://www.postgresql.org/docs/17/protocol-replication.html).

Pause stops replication reads, retains the slot and runs the retention monitor;
it continues holding a compute lease. It is not a zero-cost pause. Resume consumes
retained history. A worker/process restart reopens the same durable generation.
Source outages retry with backoff, under the persistent source retention cap.
A forced source suspension can leave cleanup pending until the source resumes;
status observation times expose stale workers rather than claiming healthy sync.
Normal suspend observes the capture lease.

Policy revision/lineage changes, DDL (including empty create/drop in a single
transaction), lost WAL/slot, source acknowledgment ahead of the spool, corruption,
unsupported messages and budget exhaustion fence the generation as
`resync_required`. Owned resource cleanup runs separately from spool validation,
so a corrupt journal cannot prevent slot release. Last observed boundaries are
retained for diagnosis. A new baseline requires explicit delete and new enrollment;
there is no silent gap skipping or full-refresh fallback.

Delete removes the private journal only after the worker has stopped, source
cleanup has completed (or branch deletion is confirmed), and bootstrap cancellation
is terminal. A failed cleanup remains visible and keeps admission reserved.
Backup restore fences all copied capture intent; it cannot silently resume a copied
cursor against live source history. Catalog schema 24 uses the existing stopped,
backed-up upgrade path, including from schema 23.

## Qualification and remaining gates

`e2e/native/capture.py` qualifies real native PostgreSQL, the daemon and the locked
analytical runtime. Ten local Linux checks passed: isolated bootstrap with a
long multi-table transaction spanning F; durable acknowledgment/abort behavior;
worker SIGKILL and daemon/compute restart; pause/resume; corrupt-spool cleanup;
empty create/drop fence; lost slot; acknowledgment ahead of the spool; paused WAL pressure; and rejection of a user `pgapp` schema without misclassifying it as system metadata. The same harness passed on Linux/macOS in [native-cell run 35786273435](https://github.com/supabricks/platform/actions/runs/35786273435), at head `a951180d56b8c119bc67002ed817ba6bc346939b`. Required merge checks also passed.

`python/analytics/test_capture.py` exercises process exits before/after spool commit
and feedback, duplicate replay, chain corruption, identity/ownership, simulated
free-space exhaustion, decoder limits and rejection before wire allocation.
Rust tests cover admission/retry/project boundaries, policy revision fencing,
stale status, restored intent and catalog upgrade. These tests do not claim
physical power-loss durability or qualify a newly assembled release archive.
