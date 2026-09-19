# Plan and apply a project

Start Supabricks and explicitly create or attach a format-2 deployment as described
in [deployment bindings](project-deployments.md). Review a plan before applying it:

```sh
supabricks project plan --project ./sales > /tmp/sales-plan.json
supabricks project apply /tmp/sales-plan.json --key sales-v1 --project ./sales
supabricks project status OPERATION_UUID --project ./sales
supabricks project installed --project ./sales
```

Keep the plan file outside the declared source inventory. It includes the exact
source/package hashes, destination revision and local actor. Apply returns an
operation ID immediately; poll until `state` is `succeeded`, `failed` or `cancelled`.
After a lost response, `project find --key sales-v1` recovers it. Reusing the same key
and plan is safe; a different plan needs a new key. A stale plan must be regenerated.

Apply creates declared fresh PostgreSQL databases, installs immutable SQL/notebook
source and prepares declared Python environments **offline**. Declared migrations
and bounded fixtures execute as explicit reviewed initialization steps; saved
queries and notebook cells run only on request. The inspection-only sales example
needs a compatible locked notebook environment before apply; for the qualified
baseline use the declaration pair supplied in
`current/python/notebooks/environments/base/` of your local installation. Arbitrary
lockfiles require their dependencies to be available to the NE preparation service;
[offline project closure and initialization](project-offline.md) supplies target
wheel bundles, migration receipts and bounded fixture loads.

## Adopt an existing database

A matching name never authorizes adoption. Put the intended branch UUIDs in an
explicit JSON mapping, scoped to this deployment's runtime project:

```json
{"database.main": "EXISTING_ROOT_BRANCH_UUID"}
```

```sh
supabricks project plan --project ./sales --adopt /tmp/bindings.json > /tmp/sales-plan.json
```

Review and apply that plan with a new key. Owned databases are retained on later
applies. Removing a declaration does not delete its database or installed asset.
Changing an owned database's binding is refused.

## Work with installed notebooks and queries

`project installed` reports the active revision, logical resources and
`environment_worktrees`. Each environment worktree contains editable notebook
copies named by their logical key, such as `notebooks/sales.ipynb`:

```sh
supabricks branch use main --project /PATH/FROM/environment_worktrees
supabricks console --project /PATH/FROM/environment_worktrees
```

The existing console opens those drafts and uses the prepared environment. A later
apply creates a new revision and working root; already running kernels keep their
existing environment. SQL assets can be read without executing them:

```sh
supabricks project asset query.sales_total --project ./sales
supabricks project draft query.sales_total --path queries/experiment.sql --project ./sales
supabricks project draft notebook.sales --path notebooks/experiment.ipynb --project ./sales
```

Draft paths must be new, stay within the checkout and contain no symlink components.
Edits and execution outputs never change the installed archive. The console's
Project packages workspace provides preview, binding, reviewed apply, progress
and installed asset/draft controls.

## Failure, cancellation and recovery

```sh
supabricks project cancel OPERATION_UUID --project ./sales
supabricks project status OPERATION_UUID --project ./sales
```

Cancellation stops further work and retains allocated databases. Failed or cancelled
applies keep the previous active revision; their journals report partial resources.
Inspect the diagnostic, fix the source or dependency problem, then create a fresh
plan and key. Database allocation retries use stable child keys across daemon crashes.
No automatic deletion or rollback of already committed database effects occurs.

Catalog 13 requires the normal stopped-backup upgrade from an older installation.
Stopped backups preserve revision archives and draft worktrees. Virtual environments
are rebuilt after a cold restore; `project installed` reports `preparation_needed`.
Retain the original source checkout and exact previous release for recovery.
