# Offline runnable projects

Use the matching native Supabricks release on each target. Build the dependency
bundle on a connected preparation machine when dependencies need downloading;
installation and notebook launch on the destination use the locked offline bundle.

## Prepare and package

Start with the shipped `examples/projects/sales-runnable` template. The release ships it with the qualified base declaration pair. The repository
copy uses minimal inspection fixtures; replace those with the release pair or a
prepared custom pair before deploying.

For the shipped base environment, copy the release's
`python/notebooks/environments/base/{pyproject.toml,uv.lock}` into the project's
`notebooks/environment/`. Bind a separate build worktree to a deployment, prepare
its environment and export it:

```sh
supabricks env prepare --wait
supabricks env export-bundle /absolute/path/kernel.zip --offline --wait
```

Copy `kernel.zip` to `dependencies/kernel.zip` in the source project, include
`dependencies/*.zip` in `package.include`, then declare the target:

```toml
[environments.notebook.bundles]
linux-x86_64 = "dependencies/kernel.zip"
# macos-arm64 = "dependencies/kernel-macos.zip"
```

The bundle's declaration pair must be byte-identical to the project's pair.
Export another bundle on macOS for macOS. Both must match their target release's
kernel contract. `project inspect` reports the included closure per target;
`project plan` rejects a missing target or wrong contract. Locked offline
preparation establishes readiness before activation.

```sh
supabricks project pack --project ./sales --output /tmp/sales.sbproj
supabricks project verify /tmp/sales.sbproj
supabricks project unpack /tmp/sales.sbproj --destination ./sales-local
supabricks project create --project ./sales-local --key sales-local
supabricks project plan --project ./sales-local > /tmp/sales-plan.json
# Review the database, migration, fixture and environment steps.
supabricks project apply /tmp/sales-plan.json --project ./sales-local --key sales-v1
supabricks project status OPERATION_ID --project ./sales-local
supabricks project installed --project ./sales-local
```

Packing/unpacking is offline and grants no destination authority. `project create`
explicitly selects a new deployment; attaching a checkout to an existing deployment
is a separate choice. Keep plan files outside selected package inputs.

## Initialization and querying

The sales template declares two ordered migrations and an explicitly mapped CSV
fixture. Apply creates `public.sales` containing two rows with total amount 30.
Migrations use one statement per file and commit independently. For a schema/data
change, add a new migration with a higher sequence. Editing a committed migration
is rejected. Fixture loads create fresh tables; they do not append or overwrite.
Use a new logical fixture and table for different fixture data.

```sh
supabricks sql --project ./sales-local --branch main --sql 'SELECT sum(amount) FROM public.sales'
supabricks analytics open --project ./sales-local --branch main --wait
supabricks analytics sql --project ./sales-local --branch main --sql 'SELECT sum(amount) FROM public.sales'
```

`project installed` reports `environment_worktrees`. Open the reported notebook
worktree with `supabricks console --project PATH`; choose main and execute the sales
notebook. That kernel uses its prepared environment and a pinned Spark snapshot.
Apply never runs notebook cells or saved queries automatically.

If apply fails, inspect its step and receipts. Earlier committed migrations and
loads remain even while the previous revision stays active. Replan and apply with
a new key to reconcile identical migration/fixture identities. An interrupted
migration with an unknown commit outcome checks its PostgreSQL receipt before
executing again. For a failed fixture, inspect `ingest status JOB_ID`, then use
`ingest retry JOB_ID` only when retryable before reapplying. Cancellation waits for
a current migration transaction and ingestion reconciliation; it is not rollback.
Resume an existing database and wait before planning initialization against it.

The current local-owner model trusts reviewed project SQL. This is not multiuser
isolation or a substitute for future project RBAC.
