# PK03 — Destination-owned deployment bindings

Implemented contract; merge and native archive qualification are tracked in the
implementation PR. [User workflow](../handbook/project-deployments.md) ·
[Packaging plan](../plans/project-packaging-implementation.md).

PK03 separates portable definition UUIDs from runtime project UUIDs. A definition
can have multiple deployments in the destination's default local workspace. Each
new deployment allocates an independent runtime project; existing branch allocation
therefore allocates independent PostgreSQL tenant/timeline identities. Creating a
deployment creates metadata only. It does not execute resource declarations, SQL,
notebooks or package resolvers. PK04 supplies plan/apply.

## Catalog 11 and identities

Catalog version 11 adds realms, workspaces, principals, project definitions,
deployments, private worktree bindings and binding-operation replay records.
One local realm, workspace and local-owner principal are generated and persisted
per cell. Deployment UUIDs identify instances; `runtime_project_id` maps to the
existing `projects.id`. Every legacy project receives one legacy deployment whose
definition UUID and runtime UUID remain its original public UUID. Branches,
endpoints, tenant/timeline IDs, queries, selections, operation records and notebook
environment records are not rewritten.

Known legacy worktrees are the union of branch selections, environment generations,
environment operations and active environment selections. Conflicting project
claims for one path abort the whole migration. A legacy project with no persisted
worktree evidence remains unbound until the local owner explicitly attaches it.
The migration cannot safely infer its old checkout from a copied manifest UUID.

Bindings store canonical paths, source format and directory device/inode identity.
Existing migrated paths are pinned on first admission, because prior catalogs did
not record inode identity. New attachments pin it immediately. A moved checkout
requires explicit attachment at its new path; a directory replaced at an existing
path is rejected until explicitly reconfirmed. Label edits never change IDs.
Separate attached worktrees share deployment resources but keep their existing
path-scoped branch/environment selections. Moving a worktree does not relocate
its old environment generations; they remain retained under their original scope.

Only the existing stopped-backup upgrade can migrate a populated catalog. Ordinary
startup refuses a mismatched schema without migrating. Catalogs 8, 9 and 10 can
upgrade to 11; exact-source restore retains the original schema and release before
upgrade. Migration is transactional, recorded against the verified source backup
hash and candidate release identity, and resumes through the existing upgrade
journal. Alpha.20 declares catalog 11; the installed server is not upgraded merely
by merging this implementation.

## Central admission

The daemon resolves a fixed source identity/worktree to a destination binding.
CLI clients resolve before constructing runtime requests. MCP remains usable for
offline source inspection, then resolves before runtime calls. Console launch,
overview/actions, notebook transport/admission and environment admission validate
the same binding. Branch selection checks the private deployment mapping instead
of equating the manifest UUID with a runtime project UUID. Console overview and
API capabilities expose the resolved context; source inspection still reports
portable definition identity only.

A genuinely new format-1 definition keeps the existing `init` plus first-use
experience: the daemon atomically registers its legacy runtime/deployment and first
worktree. An existing definition or runtime UUID cannot use this path. A copied
format-1 or format-2 checkout must explicitly attach or create a deployment.
A source-carried marker or UUID never supplies the private binding.

The low-level private control socket remains an OS-owner administration interface;
it is not a tenant security boundary. This change centralizes application binding,
not multiuser execution isolation. Actor and effective-principal fields are
assigned by the destination's local-owner provider and retained on deployment,
worktree and binding-operation records. The API accepts no actor override. There
are no passwords, external identity providers, roles, grants or Unity Catalog calls
in this slice. OSS Unity Catalog/IAM remain follow-on integrations.

## Explicit lifecycle

- `project create --key KEY`: validate a format-2 source, allocate a new deployment
  and runtime UUID, and attach the current unbound worktree. A selected target is
  recorded; development/production modes confer no authority.
- `project deployments` / `project binding`: list deployments of the fixed source
  definition, or inspect this worktree's binding without silently attaching it.
- `project attach DEPLOYMENT_ID`: attach another checkout of the same definition.
  Existing different bindings are rejected. Repeating the same attachment is safe;
  it can explicitly reconfirm a replaced directory. Format-2 attachment to a legacy
  deployment requires adoption first.
- `project adopt RUNTIME_PROJECT_ID --key KEY`: adopt an existing legacy runtime
  using a reviewed format-2 manifest with its original public UUID. Preserve the
  deployment/runtime identities and all resources; increment the deployment
  revision, record the target, and explicitly accept the source format change.
- `project fork --destination NEW_DIRECTORY --name NAME`: copy validated portable
  source through PK02's stripping/private publication path with a new public
  definition UUID. Record public origin provenance and leave it unbound. The
  original is unchanged; nothing is deployed automatically.

Create/adopt keys are unique per definition in this cell. Their replay records
include the canonical worktree, command/target and source inventory digest. The
same request recovers the original deployment; changed inputs conflict. These
metadata mutations and attachment commit in one SQLite transaction. No external
resource has to be rolled back. Attach refuses switching a bound worktree to
another deployment; use a separate checkout. Detach/rebind, workspace management,
resource adoption with a different public UUID, and source apply are not implied.

## Evidence

Portable tests exercise two deployments of one definition with different runtime
and tenant IDs, copied-UUID rejection, explicit multi-worktree attachment with
independent selections, adoption preserving saved-query bytes and active notebook
environment/lock identity, restart behavior, moved/replaced directories, fixed MCP
source identity, console overview context, forged runtime/actor arguments and
replay conflicts. Catalog-10 migration preserves existing rows, rejects ambiguous
legacy worktrees atomically, and runs through all stopped-upgrade interruption
boundaries alongside catalogs 8/9. Native qualification adds real isolated
PostgreSQL databases and explicit attachment to the installed archive workflow.
