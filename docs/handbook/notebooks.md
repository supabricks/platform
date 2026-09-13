# Local notebooks

Run `supabricks up`, then `supabricks console`. Open **Notebooks**, choose a
branch and click **Start kernel**. The console uses a JupyterLab notebook editor
with the platform's bundled Python kernel and Sail session; no separate browser
Jupyter server or Python installation is needed for an installed release.

Kernels run in managed per-project virtual environments. On the first explicit
start, Supabricks prepares its bundled offline default. No host Python or package
index is needed. Commit `notebooks/environment/pyproject.toml` and `uv.lock` with
your project. The **Python environment and packages** panel shows declared and
installed dependencies. Add or remove packages there, then explicitly choose
**Use prepared environment** to restart Python with the prepared versions.
Variables are discarded; the analytical snapshot stays pinned and saved cells
are not replayed. Snapshot refresh is a separate action.

**Offline packages only** starts enabled. Disable it to allow downloading registry
wheels from PyPI, or import an offline bundle in the panel's diagnostics section.
**Prepare environment** prepares the current lock. Progress, cancellation and
errors appear in the panel without discarding notebook edits. Missing locks must
be restored alongside the reviewed manifest before preparing. The `supabricks env`
CLI offers the same package operations; use these controls instead of `%pip` or
`%uv`. Project packages apply to notebook Python, not Sail execution workers.

The [environment recovery guide](notebook-environments.md) covers upgrades, offline bundles, moved projects and package compatibility.

See the [console environment workflow](../architecture/ne05-console-environments.md)
and [kernel environment contract](../architecture/ne03-kernel-environments.md).

## First query

Import the orders CSV through the console, or create `public.orders` using SQL.
Add this Python cell, then click **Run cell** (or press Shift+Enter):

```python
spark.sql("SELECT * FROM public.orders LIMIT 20").show()
```

For an imported `amount` column stored as text, cast it when aggregating:

```python
spark.sql("""
    SELECT SUM(CAST(amount AS DECIMAL(18, 2))) AS total
    FROM public.orders
""").show()
```

**Add Python cell** and **Add Markdown cell** extend the notebook. **Run all**
executes cells sequentially and stops on the first failure or interrupt.
Python variables remain in that kernel until it stops, expires or restarts.

## Files and edits

**Save notebook** writes a standard `.ipynb` under the project's `notebooks/`
directory. Existing `.ipynb` files placed there appear after **Refresh files**.
**Save a copy**, **Rename notebook**, and **Download notebook** are available.
Nested relative filenames are supported; symlinks and traversal are rejected.
The [orders example](../../examples/notebooks/orders.ipynb) can be copied there.

Sources and Markdown are saved by default. Check **Include outputs when saving
or downloading** to retain results. All imported/generated output is treated as
untrusted display content. Supported output is plain text, stdout/stderr,
sanitized HTML/Markdown and static PNG/JPEG. JavaScript, SVG, widgets and remote
images are not enabled. Python itself is trusted local execution with the
runtime user's permissions, not a sandbox for untrusted notebooks.

Unsaved changes remain in the editor across console navigation. Opening another
file or closing the page asks before discarding them. If a file changed on disk,
save reports a conflict and keeps your edits. Download them or save under a new
name, then reopen the current disk file. A new file never replaces an existing
name implicitly. Files are not autosaved into browser storage.

Saved notebooks are application source files: commit or back them up with the
project. Platform runtime backups do not include them or live Python variables.

## Snapshots and kernels

A kernel is pinned to one branch and analytical epoch. PostgreSQL writes do not
immediately appear in that running kernel. **Refresh snapshot** publishes a new
analytical epoch and shows progress; the current kernel keeps its original one.
**Start on latest snapshot** explicitly replaces the kernel to see the new data.
**Restart kernel** resets Python variables while retaining both the pinned epoch
and environment. **Use prepared environment** selects the project's prepared
environment and retains the epoch. Neither action automatically runs saved cells.
Changed declarations leave existing kernels untouched. Environment identity is
shown with the snapshot and retained in each saved cell's output provenance.
Changing the branch selection takes effect only when you start/rebind a kernel.

Saving records the selected binding. Reopening never starts a kernel or runs
code. Starting a saved notebook requests its saved epoch; a missing branch or
unavailable epoch is an error, not a silent switch to latest. Select an available
branch and explicitly choose latest when that is the intended recovery.
The console displays the current epoch, observation time and expiration.

Output can outlive its kernel. The most recent execution's snapshot is shown
separately from the current kernel. With output persistence enabled, individual
code cells retain provenance in `metadata.supabricks_outputs`; a document may
contain results from different epochs when only some cells were rerun.

**Interrupt** requests cancellation and waits for the observed execution result.
**Stop kernel** releases its runtime resources. After a channel loss, results
may be incomplete. **Reconnect kernel**, or **Attach** after reopening the page,
connects explicitly without rerunning cells. There is no durable replay of
missed output: inspect state and decide which cells to run again.

## Preview limits and evidence

Documents: 8 MiB, 512 cells, 512 KiB source per cell. A submitted Python cell is
limited to 64 KiB; browser output is bounded to 1 MiB per execution with a 2 MiB
frame ceiling. Directory traversal is limited to 4096 entries. Runtime admission,
expiration and process limits follow the [N02 contract](../architecture/n02-notebook-runtime.md).

Chromium is the current browser qualification target. See the
[repair record](../reviews/n00-n06-repairs.md) for tested behavior and pending
installed-platform qualification; a passing source-build test alone does not
establish Linux/macOS release readiness.
