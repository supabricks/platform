# PK05: offline project closure and initialization

PK05 extends PK04's reviewed plan and journal. It uses the NE04 bundle importer,
the I03 ingestion worker and native PostgreSQL transactions. It does not run
notebook cells, source hooks, dependency resolution, arbitrary jobs or application
servers during deployment.

## Dependencies and portability

An environment may declare `bundles`, mapping `linux-x86_64` and/or `macos-arm64`
to explicitly included `dependencies/*.zip` files. These are actual NE04 exports,
not materialized virtual environments. Source inspection verifies the bounded ZIP
inventory, every member hash, target, kernel-contract digest and exact declaration
pair. Its per-target closure report deliberately says
`inventory_verified_runtime_preparation_required`: only target runtime preparation
can establish wheel compatibility, locked dependency closure and kernel readiness.

Planning selects the current native target and checks its kernel contract. If any
bundles are declared, a missing target fails rather than silently using a cache.
Apply imports the matching archive offline into its private environment worktree.
NE04 performs uv's locked export and verified wheel-only installation, without
online resolution or build hooks. Preparation precedes database creation. Activation
checks the generation's declaration hashes against the immutable source package.
Environments without declared bundles retain PK04's offline preparation behavior;
a custom lock may require artifacts that are absent on another machine.

Ordinary source retains its 8 MiB file / 32 MiB total budgets. Explicit dependency
ZIP paths allow 128 MiB each / 256 MiB total, within a 300 MiB compressed and
expanded archive envelope. Declarations retain their 1 MiB limit. All descriptor,
no-symlink, no-overwrite, path and inventory checks remain. Old source-only package
bytes and format version 1 remain unchanged. Older binaries reject new manifests
or oversized packages; there is no claim they can execute PK05 resources.

## Database initialization

A `migration` resource names a `.sql` file, database and positive `sequence`.
Sequences are unique per declared database and add dependency edges in ascending
order. Each file contains one PostgreSQL statement of at most 32 KiB. It must
start with CREATE, ALTER, DROP, INSERT, UPDATE, DELETE, COMMENT, GRANT or REVOKE;
leading comments, transaction/session commands, CALL, DO and COPY are excluded.
PostgreSQL extended Parse rejects multiple statements before execution. Statements
that PostgreSQL forbids inside a transaction fail normally. A project can use
multiple ordered files for a multi-step schema change.

Each migration runs on a bounded background worker (four per cell, 30 second total
deadline and the existing 1 MiB PostgreSQL frame limit). A database advisory transaction lock serializes the private receipt
check, user statement and receipt insert. SQL runs as `supabricks_owner`; receipts
are owned by the internal `cloud_admin` role in `_supabricks.project_migrations`.
The shared schema and ingestion receipt table retain I03's application-owner
contract; the migration receipt table is created as the internal role.
Their identity includes cell origin, deployment, branch, logical resource, sequence
and source SHA-256. The same identity/checksum is a no-op after a committed receipt;
changes or retroactive sequences fail. A lost connection or COMMIT reply is an
unknown outcome, resolved by a later attempt reading the same PostgreSQL receipt.
These are trusted, explicitly reviewed project migrations, not a SQL sandbox or
an RBAC boundary. Application-defined functions may have effects outside a database
transaction; projects must not use them for deployment hooks.

A `fixture` resource names a source file, database, schema, new table and explicit
I03 mapping. CSV/TSV, JSON lines/array/document and Parquet retain the existing
parser and load limits; there is no sample-inferred mapping or append/replace mode.
The package's source limit additionally bounds fixtures. Distinct fixtures must
name distinct destination tables. A stable deployment/logical-resource key finds
the original ingestion job across project apply retries. Changed input, mapping
or destination conflicts. PostgreSQL table creation, load and ingestion receipt
already share one transaction; recovery uses the existing receipt reconciliation.
A failed ingestion must be explicitly retried when its status says retryable.
A succeeded receipt records a historical load, not continuous enforcement of table
contents against later user edits.

Initialization is allowed only in newly created or already deployment-owned
databases. Adopt an existing database in a separate reviewed apply first. Existing
databases must be running and settled before planning initialization. Pending
applies protect prepared databases against lifecycle changes, including force;
cancellation waits for a running migration and for ingestion reconciliation before
acknowledging a terminal state. Shutdown waits for bounded migration workers.

## Commit boundaries and recovery

Each migration and fixture is a separate committed step, visible in operation
resource receipts. The active deployment pointer changes only after all steps
succeed. Failure preserves the previous pointer and earlier committed database
changes; there is no cross-database transaction or automatic DDL rollback. Error
responses identify the current step and distinguish unacknowledged SQL outcomes.
Cancellation is between migration transactions and cannot undo committed work.
No notebook is executed on apply or recovery.

Catalog 13 fences the expanded JSON journal contract from older binaries, even
though no new SQLite tables are needed. Backed upgrades support catalogs 8–12;
exact-source restore supports 8–13. Backups retain source archives and receipts;
materialized environments remain disposable. The candidate release is alpha.22.

## Qualification

`install/native/project_offline.py` is part of exact native archive qualification
on both targets. It exports a real NE04 bundle, transfers a `.sbproj`, creates a
fresh deployment, prepares offline, applies ordered migrations and a CSV fixture,
reapplies without duplicate rows, checks failed-statement rollback, multi-statement rejection, changed
committed-checksum rejection and SIGKILL after COMMIT before journaling, and explicitly runs PostgreSQL, Spark and the saved
notebook through the console Jupyter transport. Kernel assertions retain complete
epoch and environment provenance. Network-isolated release qualification, rather
than a source-binary run with network available, establishes the release claim.
