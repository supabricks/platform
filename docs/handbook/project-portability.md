# Move a runnable project between local installations

This walkthrough uses the installed `examples/projects/sales-runnable` project:
two synthetic CSV rows (amounts 10 and 20), two initialization migrations, one
saved SQL query and one notebook. SQL and Spark should both return total 30.
Use the same Supabricks release on the preparation and destination machines.
The `PROJECT-OFFLINE.md`, `PACKAGES.md`, `DEPLOYMENTS.md` and `PROJECT-APPLY.md`
files beside this document describe the individual commands.

## Prepare the artifact

Copy the installed example into a writable `sales` directory. Keep the example's
`notebooks/environment/pyproject.toml` and `uv.lock` unchanged. On each supported
native target, create a separate export worktree:

```sh
mkdir exporter
supabricks init exporter --project ./exporter
supabricks up --project ./exporter
supabricks env init --project ./exporter --wait
supabricks env export-bundle /absolute/path/kernel.zip --project ./exporter --offline --wait
```

For this base template, `env init` supplies the same declaration pair as the
installed example. For custom dependencies, explicitly prepare and export your
reviewed pair instead. Copy each native bundle into the single source project as
`dependencies/linux-x86_64.zip` and `dependencies/macos-arm64.zip`. Add
`"dependencies/*.zip"` to its existing `package.include` list and append:

```toml
[environments.notebook.bundles]
linux-x86_64 = "dependencies/linux-x86_64.zip"
macos-arm64 = "dependencies/macos-arm64.zip"
```

Both exports must have byte-identical declarations and locks. Wheel files and
kernel contracts are native to their target. Build the project artifact **once**:

```sh
supabricks project inspect --project ./sales
supabricks project pack --project ./sales --output /absolute/path/sales.sbproj
supabricks project verify /absolute/path/sales.sbproj
```

Transfer that exact file to the second installation. Compare `archive_sha256`
from `project verify` on both hosts. A package contains reviewed source and
explicit dependency bundles; it does not contain deployment authority, database
storage, credentials, notebook outputs, active kernels or arbitrary local files.

## Install and run explicitly

```sh
supabricks project unpack /absolute/path/sales.sbproj --destination ./sales-local
supabricks project create --project ./sales-local --key sales-local
supabricks project plan --project ./sales-local > /tmp/sales-plan.json
# Review the plan, then submit it with a stable key.
supabricks project apply /tmp/sales-plan.json --project ./sales-local --key sales-v1
supabricks project status OPERATION_ID --project ./sales-local
supabricks project installed --project ./sales-local
supabricks sql --project ./sales-local --branch main --sql 'SELECT sum(amount) FROM public.sales'
supabricks analytics open --project ./sales-local --branch main --wait
supabricks analytics sql --project ./sales-local --branch main --sql 'SELECT sum(amount) FROM public.sales'
```

Wait for apply to succeed before querying. Use the reported
`environment_worktrees.environment.notebook` path to open the console:
`supabricks console --project PATH`. Open `sales.ipynb` and execute its cells.
The kernel uses the managed environment and a frozen Spark snapshot.

The console's **Project packages** page provides preview, import, explicit
binding, plan review, apply progress, installed assets and editable drafts.
Importing or previewing never executes project code. Reuse an apply key to
recover a lost response; after changing source or bindings, review a new plan
and use a new key.

## Restart, move and recover

`supabricks down` followed by `supabricks up` retains data and installed revisions.
Notebooks start only on explicit request. Save the deployment ID from
`project binding`. After moving a worktree or restoring a backup to a new data
root, explicitly run `project attach DEPLOYMENT_ID --project NEW_PATH`.

A release upgrade or cold restore may report `preparation_needed`. Retain the
source artifact, unpacked project and both dependency bundles independently of
the runtime backup. Review a new `project plan` and apply it to reconstruct the
managed environment. Migrations and fixture receipts prevent duplicate
initialization; rebuilding environments does not replay notebook cells.
For a changed kernel contract, export matching bundles with the new release
before replanning. See `RECOVERY.md` for the stopped-cell backup and signed
localhost upgrade procedure.

Cancellation and failed apply retain allocated databases and completed
initialization. Inspect the operation and installed revision before retrying;
retention is intentional and is not a transaction rollback.

## Qualification and limits

The PK07 release gate transfers one artifact from its Linux producer to separate
Linux x86_64 and macOS arm64 installations. It requires offline SQL, Spark and
notebook execution, restart, signed upgrade, cold restore, binding and relocation
checks, cancellation, interrupted apply, malformed packages, missing closure and
bounded-volume disk exhaustion. The existing browser, ingestion, storage and
notebook gates remain required by the combined R04 collector.

The report records exact source and archive identities, both wheel inventories,
declaration/lock and fixture hashes, package/expanded bytes, candidate reapply and
kernel-start times, sampled daemon RSS and sampled data disk peak. Samples can
miss short peaks and shared pages can inflate RSS. These are measurements of
this two-row example, not capacity promises for arbitrary projects. Consult the
release's `r04-evidence` artifact; a merged implementation alone does not mean
its archives are qualified.

Current enforced packaging bounds include 8 MiB per ordinary source file,
32 MiB ordinary source total, 128 MiB per dependency bundle, 256 MiB dependency
bundles total, 300 MiB archive/expanded package and 1,024 files. General logical
database export/import is PK08. Shared-user catalog governance and RBAC remain
separate workstreams. The supported local principal is the OS owner.
