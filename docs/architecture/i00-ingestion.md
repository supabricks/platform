# I00: durable ingestion contracts and catalog 9

I00 adds the storage and recovery boundary for ingestion. It does not enable CSV
loading or browser uploads: `features.ingestion` remains false. I01 supplies the
bounded acquisition/parser/COPY worker and CLI/MCP adapters; I02 supplies uploads
and the wizard. No new dependency or engine fork is introduced.

## Stored contract

`crates/local/src/ingest/` defines protocol 1 and
`crates/local/src/store/ingest.rs` owns its transitions under the existing daemon
lock. Workers never open SQLite. Catalog migration 0009 adds source/job records,
a stable origin and a migration receipt. The origin copies the existing analytical
installation identity, preserving one data lineage across release upgrades and
physical restores; it is not the changing archive checksum.

Sources have server-generated UUIDs, project ownership, acquisition generation,
a display-only filename, immutable SHA-256/size, state and expiry. Acquisition
allocates a receiving slot and a private `.part` file. Sealing verifies the bounded
regular file, synchronizes it, publishes a `.source` name without replacement,
synchronizes the directory, then commits the usable reference. A crash between
these steps leaves an unusable interrupted source. The future acquisition worker
must close all write descriptors and be fenced before sealing; no HTTP API exposes
these internal methods. Sources and directories use 0600/0700 permissions.

A load freezes project, branch UUID/revision, source UUID/hash, destination schema
and new table, and a typed mapping. A mapping fingerprint is SHA-256 of the typed
versioned JSON serialization (ordered columns and null options are significant).
There are no SQL expressions in mappings. Parsers must reject unsupported formats
and conversions until their respective slice is qualified; enum membership does
not advertise an implemented reader.

Idempotency keys are scoped to project and branch. Repeating the exact request
returns the same job even after its payload has been disposed. Changing any input
under that key conflicts. One queued/loading/reconciling job is admitted per cell.

| State | Allowed next action |
| --- | --- |
| queued | Register a current-generation gated worker, or cancel without executing |
| loading | Monotonic parsed/copied progress, or fence and enter reconciliation |
| reconciling | Remain pending on unknown; accept matching receipt; prove absence after fencing |
| succeeded | Retain compact receipt and committed row count; release source reference |
| failed | Explicit retry only after proven rollback and while source/revision remain valid |
| cancelled | Terminal; release source reference |

A queued job interrupted by restart becomes retryable failed; loading becomes
reconciling. No job runs automatically on restart. Progress cannot establish
success: committed rows stay null until a matching receipt is recorded. Worker
identity includes PID, process birth identity, secret token, branch revision and
daemon generation. Recovery stops every registered `ingest-` process group before
removing ownership and changing job state. The gated launch registration precedes
execution. Tests include a real surviving child across daemon replacement.

Cancellation during loading remains reconciling. A committed receipt wins a
concurrent cancellation. A network failure, EOF or missing worker response is
Unknown, never proof of absence. After verified fencing, absence of both receipt
and target permits explicit retry; an existing target without a receipt conflicts.
A mismatching receipt or target OID also conflicts. I01 must validate the actual
reserved schema/receipt definition and target identity before supplying evidence
to the coordinator; it must not infer absence from a query error.

## PostgreSQL receipt and branching

The canonical protocol DDL is `crates/local/src/ingest/receipt.sql`, packaged as
`share/ingest/receipt.sql` in the immutable inventory. The worker executes target
creation, COPY and receipt insertion in one PostgreSQL transaction. The receipt
primary key includes origin, project, originating branch and job UUID. It stores
source hash, mapping fingerprint, target schema/name/OID and committed row count.
Driver parameters carry values; driver identifier quoting carries names.

Physical child branches inherit `_supabricks.ingest_receipts`. Those inherited
rows retain their original branch identity and cannot satisfy a child-origin job.
The reserved schema is excluded from both PostgreSQL catalog queries and both
analytical discovery implementations, but remains in physical storage/backups.
Schema hiding is not authorization against the local filesystem/database owner.
I00's installed fixture exercises actual COPY rollback/commit, reconnecting to read
a receipt, physical inheritance, isolated writes and catalog/export exclusion.
The complete importer failure/retry qualification remains I01.

## Retention and maintenance

The provisional source ceiling is 100 MiB, aggregate reserved staging 512 MiB,
preview 100 rows/256 KiB and mapping 256 columns. Receiving/interrupted slots reserve
the full source ceiling. Preview responses explicitly say they are samples.
The worker's streaming/decoded/RSS/deadline enforcement and measurements land in
I01; these storage limits are not throughput or memory qualification claims.

Abandoned staged/interrupted sources expire after 24 hours. Failed jobs retain a
source for retry until its visible expiry. Succeeded/cancelled jobs release their
reference. Bounded daemon cleanup disposes only unreferenced or expired inactive
sources; any queued/loading/reconciling job or worker prevents disposal. Explicit
disposal revokes failed-job retry references. Disposal commits its state before
unlinking files, synchronizes directories, and records cleanup completion so an
interruption can resume safely. All cleanup holds the same root lock as backup.

Suspension/deletion, including force deletion, refuse active imports until they
are cancelled and reconciled. Shutdown fences ingestion children. Backups refuse
unresolved commits and unfinished acquisition rather than claiming consistency.
I01 must resolve pending receipts while the required compute is available before
coordinated backup can succeed. Staged-source hashes are checked when creating and
verifying backups. Retained source/job metadata is copied and restored privately.
Console credentials remain ephemeral in excluded `tmp/` storage; restoring or
restarting requires a fresh console launch/session.

## Named catalog-8 upgrade

The current release is alpha.6: catalog 9, runtime 2, PostgreSQL 17, analytical
snapshot 1. Ordinary startup refuses an existing older catalog before a writer
connection or generation increment. The installed upgrade command allows only
the named 8-to-9 transition or identical current formats. It compares the complete
engine/storage/dependency inventory after substituting just that catalog format;
other format changes, inventory changes, targets, profiles and downgrades remain
refused.

The candidate verifies both releases, stops the old runtime, obtains the root
lock, checks metadata integrity and checkpoints SQLite. It records the durable
upgrade journal and creates/verifies a stopped catalog-8 backup. Only then does
one SQLite transaction create the ingestion tables, record the backup database
hash and target release identity, and advance `user_version` to 9. It checkpoints,
rebinds runtime paths/identity, atomically activates the release link, records the
completed upgrade and removes the journal.

On resumption, catalog 8 must match the old backup hash. Catalog 9 must contain
the matching transactional migration receipt. A missing or changed backup blocks
activation. Startup remains blocked while the upgrade journal exists. Retrying
cannot run the migration twice or create a new backup from already migrated data.
Rollback restores the retained old backup into a new root with its exact old
release. Old binaries cannot open migrated roots.

Portable tests reconstruct all four durable interruption boundaries. The native
recovery gate repeats them through the signed localhost installer using the
actual qualified alpha.3 archive from run 34184744702 on Linux and macOS, and checks
project/branch/data, credential/connection and analytical epoch preservation plus
old-release restore. These are process/interruption tests, not power-loss tests.
