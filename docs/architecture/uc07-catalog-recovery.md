# UC07: catalog recovery and operational limits

A catalog backup is a stopped-cell checkpoint. The owning release shuts down
admissions, readers, publication workers, UC and PostgreSQL; recovery then holds
the data-root lock. SQLite integrity, publication/snapshot retention and installed
dataset references are checked before the backup directory is published. Pending
publication/retirement, apply or reader references must first finish or be
explicitly reconciled. Recovery does not turn incomplete work into a committed
publication.

The owned H2 file, namespace/object identities, publication journal, bindings,
retention records, snapshots, backend contract and private credentials are included
in the existing checksummed bundle. Logs, launch descriptors, temporary files and
recreated process state are excluded. H2 is opened read-only with the pinned
bundled JRE and H2 RunScript tool to validate the metastore, namespace and table
UUIDs, ownership tombstones, schemas and locations. Every retained snapshot file
is checked against its immutable manifest. The H2 file is never copied under a
live server, and external UC is never backed up by implication.

## Moved-root restore

Restore uses the exact source release and target. The existing `restore-incomplete`
guard blocks startup throughout copying and reconciliation. A single H2
transaction changes only table locations enumerated by complete owned publication
records after validating their original IDs, schemas and locations. The platform
journal then receives the corresponding locations. Unowned registrations, grants,
publication revisions and dataset receipts are preserved. This offline operation
uses the pinned H2 2.2.224 schema; it is not a general UC SQL migration interface.

There is no cross-database transaction. A failure between the H2 and SQLite
commits leaves the startup guard intact. Preserve that partial destination for
diagnosis and restore the unchanged backup to a fresh directory. No partial
checkpoint is served, and the source backup is never edited. The same rule covers
write failures and interruption during a restore.

Console-owned worktrees inside the data root are relocated only after verifying
the copied definition/format, then bound to their new directory identities.
External worktrees retain their original paths. Historical operation records are
not rewritten; excluded notebook environments require explicit preparation.
Catalog signing keys/tokens are rotated through the existing restartable protocol
before local readiness; restored data identities and grants do not create a new
authorization claim. An external-provider restore remains blocked until the
operator explicitly configures a provider and its expected metastore identity.
`catalog service restart` cannot bypass that requirement. PostgreSQL is independent.

## Versions and upgrades

`catalog-format.json` independently declares the backend/schema, exact server
commit, publication format and capability profile. The platform SQLite version
remains 15. The existing UC01–UC06 pinned backend may acquire the new sentinel;
an existing different sentinel is never silently rewritten. Incompatible backend
or server changes require a separately qualified migration or source-release
restore. Runtime inventory compatibility and downgrade refusal remain enforced.

Upgrade intent now binds the stopped H2/configuration/credential fingerprint as
well as SQLite. The pre-upgrade backup must match that state before activation,
and retries cannot silently use an older catalog checkpoint. The existing
backup-before-rebind and restartable activation protocol remains in place. Neither
ordinary shutdown nor uninstall deletes user catalog data.

## Bounds and qualification

Admission allows at most 128 namespaces, 8,192 observed asset identities, 128 live publications, 128 tables per publication,
1,024 lifetime publication journal records and 64 GiB of catalog-retained snapshot
files. Journal/tombstone exhaustion requires exporting to a fresh installation;
retirement does not erase idempotency history. The managed H2 budget is 256 MiB.
Recovery scripts are bounded to 32 MiB and a 60-second JVM deadline. The existing
256 MiB server heap, bounded workers, short HTTP deadlines, connection admission
and rotating private logs remain. Catalog status reports storage usage and the
independent recovery contract without credentials. These are local-preview
limits, not a configurable enterprise quota system.

`e2e/native/catalog/recovery.py` exercises the installed checkpoint with actual
PG, source-built Sail and source-built UC: cross-project queries before/after
moved-root restore, retained snapshots, credential rotation, corrupt H2, backend
contract mismatch, unavailable external provider, interrupted upgrade boundaries
and downgrade refusal. Linux CI also uses a dedicated 16 MiB tmpfs to inject real
ENOSPC; macOS exercises the remaining cases. Existing UC and browser suites stay
mandatory. These component fixtures do not replace UC08's exact-archive release
qualification.
