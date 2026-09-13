# Supabricks local preview: install, import, query and build

This walkthrough uses the installed release and its synthetic files. You need
Bash, curl, OpenSSL, tar and a Chromium browser on Linux x86_64 (Ubuntu 24.04 /
glibc 2.39+) or macOS 15+ on Apple Silicon. Node, a system Python, a JVM, Docker,
Kubernetes and a model account are not required. Safari and Firefox have not been
qualified; use Chromium for this preview, including on macOS.

## Install the prepared localhost release

The person preparing the demo serves the qualified alpha.17 archives using
`python3 install/native/serve.py --directory build/releases --port 8080` on the
same device. That is a build/hosting step, not a dependency of the installed
runtime. The user runs:

```sh
curl -fsSL http://127.0.0.1:8080/install.sh | bash
supabricks_program="${SUPABRICKS_INSTALL_DIR:-$HOME/.local/share/supabricks}"
. "$supabricks_program/env"
supabricks installation verify
mkdir supabricks-demo
cd supabricks-demo
supabricks init demo
supabricks up
supabricks database create main --wait
cp "$supabricks_program/current/examples/console/sales.csv" ./sales.csv
supabricks console --no-open
```

Open the returned private URL in Chromium within 60 seconds. The launch token
is single-use; run `console --no-open` again if it expires. Keep the URL private.
The console, database and base notebook environment work after the localhost
installer server is stopped and external networking is unavailable. Browser
automation and language toolchains belong to CI/build machines, not the runtime.
The production `https://supabricks.io/install.sh` endpoint is not deployed yet.

## Import and query the live database

1. Open **Database workspace → PostgreSQL**, select `main`, then **Import file**.
   Select the copied `sales.csv`. Its contents are `id,amount` with rows `1,10`
   and `2,20`; all data is fictional and Apache-2.0 licensed.
2. Set both column types to `bigint`, choose `public.sales`, review the sample,
   approve the destination, then **Create table and import**. Wait for
   `succeeded` and two committed rows. The source file stays unchanged.
3. Open a SQL tab bound to `main`, leave writes disabled, and run:

   ```sql
   SELECT sum(amount) AS total FROM public.sales
   ```

   The total is **30**. Save the query as `Sales total` if you want its text after
   reload. Saving is explicit; queries never run automatically when reopened.

## Query the same schema with Spark SQL

Switch to **Analytics**, choose `main`, and **Publish fresh snapshot**. Wait for
`published`, then **Open latest session** and wait for `ready`. Run the same
query: the total is **30**. Inspect its source observation time and epoch, then
choose **Keep result for comparison**.

Switch to **PostgreSQL**, keep the tab on `main`, explicitly enable writes, and run:

```sql
INSERT INTO public.sales VALUES (3,40)
```

The live PostgreSQL sum is now **70**. Return to Analytics: the existing session
still returns **30**. Publish another snapshot: that same session still returns
**30**. Explicitly **Open latest session** and query again: the new session
returns **70**. The retained result and each session label identify their epochs.
Close both analytical sessions when finished; two slots are shared with notebooks
and CLI. Cancel stops an entire analytical session, not just one statement.

## Experiment on a branch

In PostgreSQL branch management, create `experiment` as a child of `main`.
Select `experiment`, open a new SQL tab and explicitly enable writes:

```sql
UPDATE public.sales SET amount = amount + 5 WHERE id = 1
```

The child sum is **75**. A separate tab bound to `main` still returns **70**.
Selecting a navigation branch does not rebind an already-open SQL tab or
analytical session. To query the child with Spark, select it as the snapshot
source, publish it, and explicitly open its session. Close that reader afterwards.
There is no automatic branch merge or unbounded database diff.

## Use the notebook without installing Python

Copy the supplied unexecuted notebook into this project:

```sh
mkdir -p notebooks
cp "$supabricks_program/current/examples/console/sales.ipynb" notebooks/sales.ipynb
```

Open **Notebooks**, refresh the file list, and open `sales.ipynb`. Select `main`
and **Start kernel**. Leave **Offline packages only** enabled. The first start
prepares the bundled managed environment. Run the supplied cell explicitly; it
uses `spark.sql(...)` and returns **70** on the latest main snapshot. Saving
outputs is optional. Notebook code executes as your local user; use notebooks
you trust. Stop the kernel after the demonstration.

Commit `supabricks.toml`, notebook source, and the generated
`notebooks/environment/pyproject.toml` and `uv.lock` with your project. Runtime
backups do not include application files or Python variables. Base wheels are
bundled; additional package downloads require an explicit choice. Export an
environment bundle before moving a project with extra packages to an offline device.

## Restart and back up

```sh
supabricks down
supabricks console --no-open
```

Branches, imported tables, durable import jobs and explicitly saved query text
remain. Browser authentication and running sessions are recreated; SQL and cells
are not replayed. Select the retained query or notebook and run it explicitly.

Create a private backup outside the data directory:

```sh
supabricks backup create "$HOME/supabricks-demo-backup"
supabricks backup verify "$HOME/supabricks-demo-backup"
supabricks up
```

Backup stops the cell. It includes credentials and keys and is not encrypted;
protect it like the database. Restore into a new data root with the matching
retained release, never into an existing live root. The installed `RECOVERY.md`
and `ENVIRONMENTS.md` cover recovery and offline environment bundles. Do not
remove program release directories that retained environments or backups need.

## Limits and useful failure behavior

| Area | Preview contract |
| --- | --- |
| Device formats | CSV/TSV, JSONL, JSON arrays, JSON document-as-jsonb and Parquet; new PostgreSQL tables only |
| Source size | 100 MiB per file; JSON arrays and whole documents 10 MiB |
| Parser and preview | 256 columns; preview 100 rows / 256 KiB; every loaded row is validated |
| Import resources | One active import; 512 MiB staged payloads, decoded values and sampled worker RSS; ten-minute job deadline |
| Retention | Successful/cancelled payloads are disposed when no retained request needs them; unused/failed staging is retained up to 24 hours; explicit disposal revokes retry |
| Analytics | Read-only Spark SQL; explicit immutable snapshots; 200 rows by default, at most 1000 rows / 256 KiB results |
| Sessions | Two analytical slots shared with notebooks/CLI; workspace sessions expire after 15 minutes or two minutes disconnected |
| Types | Bigints/decimals retain exact strings; missing JSON keys become SQL NULL; nested values need jsonb mapping |
| Snapshot compatibility | JSONB and other unsupported source types block refresh; failed/cancelled refresh preserves the previous publication |

A preview is not whole-file validation. A late parse/conversion failure rolls
back the new table. After an interrupted import, inspect its durable job before
retrying; cancellation may discover a commit that already succeeded. Do not
resubmit SQL blindly after a lost response. The console reports uncertainty and
polls the original handle. Use the displayed IDs with `supabricks ingest status`,
`supabricks analytics session` or `supabricks analytics status` for diagnostics.

Recorded benchmark numbers describe synthetic fixtures on specified CI hosts,
not capacity guarantees for your device. The release's `r04-evidence` CI artifact
contains bounded measurements and fixture/release hashes. Public hosting,
publisher signing/notarization, redistribution audit and physical power-loss
qualification are separate from this localhost preview.
