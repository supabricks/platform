# Bind a project to a local deployment (PK03 preview)

A project definition describes portable source. A deployment gives that definition
an independent runtime project inside your local Supabricks cell. You can deploy
the same definition twice without sharing database state, or explicitly attach a
second checkout to an existing deployment.

Start the cell, unpack or prepare a format-2 source, then create its deployment:

```sh
supabricks up
supabricks project create --project ./sales --key sales-local --target local
supabricks project binding --project ./sales
supabricks database create main --project ./sales --wait
```

`project create` creates an empty deployment and attaches the checkout. It does not
run the manifest's SQL/notebooks or install dependencies. Existing explicit CLI
and console operations can then use the deployment. Manifest-driven plan/apply is
PK04 work; offline dependency closure is PK05 work.

The JSON context distinguishes `definition_id`, `deployment_id` and
`runtime_project_id`, plus workspace/realm IDs and the local-owner actor. Runtime
IDs and absolute checkout paths stay in the private destination catalog; they are
not written into `supabricks.toml`. Use a new request key for each intended
create/adopt operation within a definition. Retry the identical request with the
same key after a lost response; changed files, targets or paths conflict.

## Another deployment or another checkout

After unpacking the same package into a new directory:

```sh
# Independent database state, same public definition.
supabricks project create --project ./sales-experiment --key sales-experiment

# Discover this definition's existing deployment IDs.
supabricks project deployments --project ./sales-other-checkout

# Deliberately share one existing deployment.
supabricks project attach DEPLOYMENT_UUID --project ./sales-other-checkout
supabricks branch use main --project ./sales-other-checkout
```

A copied manifest is initially unbound. Attachment requires the same definition
UUID and does not inherit another checkout's branch/environment selection. A
checkout already attached to a different deployment is never silently switched;
use a separate directory. Moving a folder requires attachment at its new path.
Existing database IDs/data remain in the same deployment. Environment generations
and selections under the previous path remain retained; prepare/select the new
worktree explicitly. Changing the project label does not retarget resources.

To create a new portable definition instead of another instance of the same one:

```sh
supabricks project fork --project ./sales --destination ./sales-template --name sales-template
```

Fork is offline, preserves the original, strips notebook outputs in the copy and
assigns a new public UUID. The result is unbound until explicitly created/attached.

## Existing projects and upgrade

Catalog 11 requires the existing stopped-backup installation upgrade. Do not run a
new source binary directly against an old catalog expecting an automatic migration.
Retain the exact previous release and verified backup for restore. The installer
upgrade keeps existing runtime/branch IDs and creates legacy deployment mappings.

Existing format-1 projects remain supported. Known worktree selections/environment
records migrate automatically. If a legacy project has no persisted worktree
record, list its deployment and explicitly attach the intended checkout. A copied
format-1 manifest cannot automatically select that existing project's data.

To adopt a legacy runtime into portable format 2:

1. Read `project binding` (or `project deployments`) and retain the runtime UUID.
2. Back up `supabricks.toml`, then write a reviewed format-2 manifest with **the same
   public `id`** and explicit package/resource declarations.
3. Run `project validate`, then adopt explicitly:

```sh
supabricks project adopt LEGACY_RUNTIME_UUID --project ./existing-app --key adopt-portable
```

The format change is refused for runtime operations until adoption succeeds.
Adoption keeps runtime IDs, branch selections, private saved queries, notebook
outputs and existing environment generations. It does not execute declarations.
A different public UUID requires a new deployment; it cannot adopt the old runtime
through this command.

MCP exposes `project_binding`, `project_deployments`, `project_create`,
`project_attach` and `project_adopt` for its fixed checkout. It continues to expose
offline `project_inspect`/`project_validate` without a daemon. Runtime calls resolve
the destination ID; callers cannot override the worktree or actor.

This is still a single-OS-owner product. The local actor fields establish identity
and provenance for later governance; they do not implement IAM/RBAC or isolate
mutually untrusted users. Open-source Unity Catalog is a later integration.
