# R03: stopped-cell recovery and explicit platform upgrades

R03 adds recovery commands and a narrowly qualified upgrade path to the localhost
preview. It keeps the same PG17.8 engine, SeaweedFS and analytical components.
This is single-host recovery tooling; it does not introduce replication, a live
backup protocol, cross-platform physical restore, or major PostgreSQL upgrades.

## Recovery boundary

`supabricks backup create /absolute/new-backup` coordinates shutdown. New client
work stops, analytical workers close, computes complete their shutdown
checkpoints, and the runtime accounts for every owned process before releasing
its lock. Recovery then reacquires the permanent data-root ownership lock,
requires no recorded processes or active analytical sessions, checks SQLite
integrity and foreign keys, and completes `wal_checkpoint(TRUNCATE)`. The lock
remains held while all data files are copied and synced.

The resulting directory contains `data/` and a versioned `backup.json`. The
manifest records the source root, exact source release, catalog version, directory
inventory, file sizes and SHA-256 digests. It is written last, after the payload
and its directory entries are synced. A killed or failed backup has no complete
manifest and is rejected by verification; it never replaces an older backup.
Each backup destination must be new and outside the source data root.

The payload includes SQLite metadata and credentials, local object storage,
safekeeper WAL, pageserver state, compute data, published analytical generations,
and any interrupted publication state needed by existing recovery logic. It
excludes dead sockets, the permanent ownership lock, checkpointed control-DB WAL
and SHM files, logs and generated launch/supervisor configuration. The generated PostgreSQL `pg_dynshmem` link to `/dev/shm` is represented by an
empty private directory after all processes stop; host shared memory is never
traversed. Other file links and unknown special files are rejected. This avoids silently omitting durable data
from future layouts. Empty data directories are preserved.

All destination directories use mode 0700 and files use 0600. The backup contains
credentials and storage signing keys in plaintext protected by these filesystem
permissions. SHA-256 detects corruption; it is not publisher authentication or
encryption. Keep backups in private storage. TLS material must reside inside the
data root to be included; external certificate/key paths make backup fail.

A backup taken after orphan recovery is a stopped physical state containing WAL,
not proof of graceful checkpoints at every engine component. The engine's normal
recovery runs on startup. A successful copy is not proof of power-loss durability.

## Restore

`backup verify PATH` verifies the entire payload without starting any services.
`backup restore PATH --data-dir NEW_ROOT` verifies before creating the target and
requires the exact source release and native target. A newer R03 recovery binary
can use `--release SOURCE_RELEASE_DIRECTORY` to restore for retained R02 binaries,
which did not yet implement backup commands.

Restore never overwrites an existing data root. It holds the new root's ownership
lock and writes a durable `restore-incomplete` marker before copying. The marker
blocks startup until every copied file has been verified, metadata integrity has
passed and directory entries have been synced. After interruption, restore into
another new directory; the partial destination remains available for inspection.
Runtime TLS paths inside the source root are rebound to the destination.
Generated engine configuration is rebuilt by ordinary startup.

Project IDs, branch IDs, parent/child data, credentials, analytical installation
identity, epoch descriptors and snapshot heads are retained. Live analytical
sessions are closed rather than transferred to a different process generation.
Connection ports are retained: stop the original cell before starting its restore
on the same host. Project files and application code outside the data root are
not backup inputs; keep the corresponding `supabricks.toml` with the application.

## Upgrade transaction

The installer defaults to identical repeat installation. A version change requires
`SUPABRICKS_UPGRADE=1` and an explicit new `SUPABRICKS_BACKUP_DIR`. It verifies the
signed candidate and inventory before invoking its upgrade command. The old
release directory remains available throughout.

Compatibility is deliberately conservative: the native target and distribution
profile must match, the semantic version must increase, and the complete engine
and engine-library inventory, SeaweedFS binary, Python runtime pin, analytical
package lock, and declared data-format versions must match. Engine inventory also
binds the shipped PostgreSQL catalog/extension libraries. The stored local catalog
must be version 8 and endpoints must be PG17. Incompatible component/catalog
changes and downgrades are rejected before upgrade metadata changes; no migration
is inferred from a PostgreSQL version number alone. R01's smaller profile is not
silently converted by this transaction.

The candidate checks the previous release and runtime identity, asks that release
to stop, acquires the data-root lock, and records `upgrade.json`. It creates and
verifies the stopped recovery bundle, atomically rebinds `runtime.json`, then
atomically replaces the installation's `current` symlink. It syncs each boundary
and writes a receipt before removing the pending journal. An installation lock
serializes R03 upgrade/uninstall commands.

R03 startup checks release identity, runtime format and pending restore/upgrade
markers before opening or migrating SQLite. It also rejects a newer catalog
before changing journal mode or owner generation. Interrupted activation is
completed by rerunning the same staged installer options. The journal identifies
the previous release even if `current` already points at the candidate. The
verified backup and stopped metadata digest must still agree. If the old release
was used after an interruption before rebinding, retry with a new backup path to
capture that newer state; old backups are never overwritten.

No automatic rollback writes old files over a newer data root. Restore the
pre-upgrade bundle into a new root and start it with the retained source release.
This is also the return path for incompatible upgrades until a separate logical
export/import or engine migration is implemented.

`installation uninstall` stops the selected cell and removes the installer-owned
command links and active symlink. It retains data, backups, the harmless shell env
file, and immutable release directories so recovery remains possible. It does not
scan or delete other data roots. Reinstalling the same signed version restores
command links. Disk removal of retained versions is a separate explicit action.

## Qualification and remaining release gates

The release workflow builds alpha.3 archives and tests their real signed localhost
installers. Separate Linux/macOS recovery jobs use the exact R02 archives from
native-release run 34172472770 as predecessors. They qualify upgrade, retained
parent/child data and epochs, credential restoration into a new root, continued
writes, restoration for the old release, corrupt-backup rejection and uninstall.
All runtime operations run with external networking denied.

Each process-failure probe inserts and acknowledges a new row immediately before
SIGKILL of Postgres, compute_ctl, pageserver, safekeeper, object storage, supervisor
or daemon. It checks the acknowledged rows, branch isolation and the retained
analytical epoch after recovery. No explicit remote-flush wait is added before
these kills. Portable contracts cover partial backup publication, interrupted
upgrade states, compatibility/downgrade rejection and immutable-source failures.
Installer contracts include signature/corruption/traversal and an actual SIGKILL
while downloading a candidate, retaining the previous release and data.

Qualification reports contain explicit checks, release identities and bounded
error details. Credentials, private backup contents and raw engine logs are not
uploaded. Existing native lifecycle, disk-full, export/session, offline workflow,
size benchmarks and Kubernetes chaos/restore suites remain regression gates.

These are process-kill tests with the kernel and its caches alive. Actual OS reboot,
power interruption and durable-acknowledgment qualification remain outstanding
before public durability claims. Publisher signing/notarization, the transitive
redistribution audit, named-laptop evidence and public hosting also remain release
gates. PR/release notes identify the exact commit and completed CI reports; the
existence of this document alone is not evidence that those tests passed.
