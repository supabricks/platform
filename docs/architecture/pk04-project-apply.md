# PK04 — Project plan and journaled apply

PK04 applies a portable format-2 definition to an explicitly bound local deployment.
The source package stays separate from destination identities and installed state.
[Workflow](../handbook/project-apply.md) · [Slice plan](../plans/project-packaging-implementation.md).

## Reviewed plan

`project plan` calculates the same bounded deterministic package as `project pack`
without writing an archive, creating resources, preparing dependencies or running
project code. Its version-1 result binds the source inventory hash, stripped package
content and transport hashes, target, canonical source worktree, destination
context/revision, previous active revision, installed release identity and local
actor/effective principal. The plan contains ordered adapter steps, explicit
existing-database adoption choices and resources retained because their declarations
were removed. It is limited to 48 KiB for the local control transport.

Apply recomputes the plan before accepting it. Modified plans, changed source,
new conflicting database names, changed destination/resource revisions, another
worktree or another installed release require replanning. Callers cannot supply
identity overrides. Plans are reviewed data, never executable command lists.
The daemon assigns the local owner; IAM and OSS Unity Catalog remain separate work.

## Journal and activation

Catalog 12 adds apply operations, logical-resource ownership, immutable deployment
revision records and one active pointer per deployment. The existing stopped-backup
upgrade supports catalogs 8–11. Ordinary startup still refuses implicit migration.

The request key is unique within a deployment. An identical retry returns the
original journal entry, including after source changes or successful activation.
Changing the plan with the same key conflicts. `project find --key` recovers a lost
reply. A partial unique index permits one pending apply per deployment.

The journal commits before any effect. The daemon advances bounded checkpoints:

1. Publish the verified source archive into a private revision directory. Reject
   source changes observed before this publication. Once staged, subsequent edits
   are drafts for a later plan; this operation continues from its verified snapshot.
2. Prepare declared environments offline through the existing NE journal. Each
   revision/environment gets a separate working root and generation namespace.
3. Create databases using stable child-operation keys, or explicitly adopt/retain
   destination branch UUIDs at their expected revisions. Persist allocation ownership
   even when an apply is later cancelled or fails.
4. Record immutable SQL/notebook asset references. Do not execute their contents.
5. Revalidate the archive, database readiness/revisions, environment generation and
   installation. In one SQLite transaction, insert the immutable revision, update
   logical resource references, advance the deployment revision and active pointer,
   and mark the operation succeeded.

No external work occurs inside the activation transaction. Each database allocation
is recoverable from its child journal if the daemon dies before recording ownership.
Every subsequent adapter checkpoint can be replayed. Interrupted environment work
uses NE recovery: an interrupted preparation can fail with a recoverable diagnostic;
it never activates the incomplete deployment. A new reviewed plan can reuse retained
databases and prepare a new environment. The existing request continues reporting
its actual terminal outcome rather than silently starting different work.

Cancellation stops additional steps, requests cancellation of environment children
and keeps previous active state. Already allocated PostgreSQL resources and their
child lifecycle journals remain retained. No rollback of committed database effects,
delete, detach or implicit rebind is promised. Missing declarations retain owned
resources and their previous asset origins. Garbage collection is a future explicit
operation; this slice retains old revisions and environment working roots.

## Installed source and drafts

The private `source.sbproj` archive is the immutable installed source. It is verified
before staging, activation and asset reads. SQL assets retain their engine and logical
database association; notebooks retain database/environment associations. They do
not inherit a session, connection URI or analytical epoch from the source package.

Each prepared environment working root receives stripped notebook draft copies at
`notebooks/<logical-name>.ipynb`. `project installed` reports those local worktree
paths. They use the existing console/notebook/environment APIs; opening the console
and selecting a branch remains explicit. Editing or executing these copies changes
the draft, never the installed archive. Running kernels retain their existing NE
leases when a later apply publishes another revision in another worktree.

`project asset` reads installed source. `project draft` copies it into a new file
under the calling checkout's `queries/` or `notebooks/`, using descriptor-relative,
no-symlink, no-overwrite publication. Source checkout edits and saved console queries
remain drafts. [PK06](pk06-console-projects.md) surfaces source versus installed
revision directly in the console using the same planning implementation.

Physical backup includes revision archives and draft worktrees. Materialized virtual
environments keep NE's rebuild-on-restore contract. `project installed` reports when
preparation is needed; replan/apply to rebuild using retained source and databases.
A portable `.sbproj` does not include destination journals, identities or virtualenvs.

## Boundaries and evidence

This is local-owner, retain-only application of databases, source assets and offline
managed environments. [PK05](pk05-offline-projects.md) extends this slice with SQL
migrations, fixtures and portable wheel closure.
There are no install hooks, notebook execution, external jobs or remote destinations.
The package's locked dependencies must already be available to the existing offline
NE preparation service; unresolved dependencies fail before activation.

Tests cover read-only deterministic plans, stale/forged inputs, explicit adoption,
serialized applies, retry recovery, retained cancellation/declarations, immutable
assets and safe drafts, stopped backup/restore and SIGKILL at intent, archive,
staging, child allocation, ownership, environment submission, adapter, prepared,
activation-transaction and completion boundaries. Installed qualification adds real PostgreSQL and a real
bundled Python environment, checks generation readiness and notebook draft isolation.
