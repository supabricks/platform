# Create projects in the console

Run `supabricks console` to open the local application. Outside a project directory,
this opens the console home and starts the installed runtime automatically. No
project files are written into your current directory.

Choose **New project**, enter a name, and choose **Create and open**. Supabricks
creates the project, provisions a PostgreSQL `main` database, selects it and opens
the workspace. You do not need to run `init`, `up`, `database create` or
`branch use` separately. Names use letters, numbers and hyphens, up to 40 characters;
uppercase letters are normalized to lowercase.

**New project → Your projects** also opens an existing browser-created project.
Projects are saved on this device, survive runtime restarts and use the same
format-2 definitions and deployment journal as CLI-created projects. Their local
source directory is shown in the overview; it is safe to inspect and edit with
your editor. The application chooses a private directory, so the browser never
needs permission to write an arbitrary host path.

Setup is durable. If a reply is lost or the page reloads, reopen **New project**
and choose **Continue setup**. This reconciles the original project and operation.
A failed operation offers **Retry setup** with the same project identity and
retained resources. It does not replace an existing database. Public setup IDs
are stored locally in the browser; connection credentials and launch secrets are
not stored there. A closed browser does not cancel the daemon's setup operation.

## Every asset belongs to a project

The console home creates and opens projects. It cannot create databases, notebooks,
queries, ingestion jobs or analytics sessions. Asset work happens inside the
selected project, and backend requests remain bound to that project's deployment.

| Asset | Project boundary |
| --- | --- |
| Databases and branches | Runtime project ID, independent of their display names |
| Tables and imported data | Destination database/branch in that project |
| Ingestion sources and jobs | Project ID and validated destination branch |
| Saved PostgreSQL queries | Project-owned inventory and branch binding |
| Notebook documents | Selected project's worktree; paths cannot escape it |
| Notebook kernels and environments | Project, worktree, branch and environment identity |
| Spark sessions, queries and published snapshots | Project and branch; session ownership is also checked |
| Portable packages | Source definition ID; destination runtime IDs are allocated locally |

Projects may both have a database named `main`, a query with the same title, or a
notebook named `analysis.ipynb`. The names do not connect their assets. Opening a
second project does not grant its session access to the first project's assets.
Future scheduled Spark jobs and other asset types must carry the same mandatory
project ownership; a global unassigned-asset area is not part of the model.

Notebook environment preparation remains available through **Notebooks** in the
console. It is not required to create a database project. Spark SQL runs against
an explicitly selected project database and its analytical snapshot.

These boundaries support the current local OS owner. Shared-user IAM, RBAC and
untrusted code isolation remain separate workstreams. Project scoping does not
claim to sandbox notebook code from its host OS account.

## Current limits

There can be 32 browser-created projects per local runtime and four live console
bridges. The browser reports capacity errors; existing projects and data are
retained. Browser-managed creation currently uses the fixed PostgreSQL `main`
starter; importing an existing `.sbproj` remains under **Project packages**.
