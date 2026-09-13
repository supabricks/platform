# Notebook environments: packages, upgrades and recovery

An installed Supabricks release includes Python, uv and the base kernel wheels.
Open the console and click **Start kernel** to prepare the base environment
offline. Opening a notebook only reads it. Notebook code runs as your local user;
the virtual environment isolates dependencies, not filesystem or network access.

## Add a dependency

In **Python environment and packages**, disable **Offline packages only** for
an explicit PyPI download, add `humanize==4.13.0`, and wait for preparation.
Click **Use prepared environment** to restart Python with that version. This
discards variables and keeps the analytical epoch. Run cells explicitly.

The equivalent project-scoped CLI preparation is:

```sh
supabricks env init --wait
supabricks env add 'humanize==4.13.0' --wait
supabricks env status
```

Initialize only when the declaration directory is absent. Keep the existing pair
otherwise. The CLI prepares an environment; use the console to explicitly adopt
it in an existing kernel. Commit both `notebooks/environment/pyproject.toml` and
`notebooks/environment/uv.lock`. Review the requested dependencies and exact
resolved versions together. A lockfile records identities and hashes, not wheel
bytes. Packages are applied to notebook Python; Sail worker/UDF dependencies
remain the bundled Sail runtime's responsibility.

Use the managed controls instead of `%pip`, `%uv`, shell installs or editing a
materialized venv. `env remove PACKAGE --wait` prepares a replacement. Existing
kernels keep their selected versions until explicit adoption. `env status`
includes operation IDs, progress/errors, installed versions recorded during
preparation and the active generation. A failed or cancelled operation does not
authorize a new generation. Inspect the operation before retrying with a new key;
reuse the original key only to recover the same request's outcome.

## Keep an offline recovery bundle

After preparing the intended lock on the target release and OS, export to a new
absolute path whose parent is canonical (use `pwd -P` if `/tmp` is an alias):

```sh
supabricks env export-bundle "$(pwd -P)/notebook-environment.zip" --offline --wait
```

Keep the ZIP with the project's source backup. It contains the complete selected
wheel set, declaration pair, hashes, target and kernel-component identity.
Export refuses an existing destination. A package acquired earlier may still be
missing from the disposable cache; an offline export then fails. Explicitly
prepare online before exporting, or retain an earlier verified compatible bundle.

On another directory or machine running the **same target and compatible kernel
component**, restore `supabricks.toml`, notebooks and the declaration pair, then:

```sh
supabricks up
supabricks env import-bundle /absolute/canonical/path/notebook-environment.zip --wait
supabricks env status
```

Import always operates offline and builds a fresh venv. It does not copy a venv
or start a notebook. If the declaration pair is absent in a fresh project, run
`env init --wait` first. Choose the prepared environment explicitly in the
console. Saved outputs keep their original provenance; no cells are replayed.
An environment ID from a moved worktree or restored data root is not reusable.

For database data and analytical epochs, separately use `backup create`, `backup
verify` and `backup restore` from the [recovery runbook](recovery.md). Those
backups omit project source, notebook documents, venvs, caches and live Python
variables. Stop the original cell before starting its restored copy so retained
ports are free. A restored saved epoch remains usable only while that epoch is
available in the restored platform data.

## Upgrade and recover

The signed upgrade installer stops kernels, creates a verified stopped backup
and retains prior program releases. After `up`, inspect `env status`. An
environment belongs to its exact installation and interpreter path, even when
package versions happen to match. Prepare again and explicitly select the new
environment; Supabricks refuses an old incompatible generation.

For an unchanged kernel component with complete wheel artifacts:

```sh
supabricks env sync --offline --wait
```

If artifacts are absent, import a compatible bundle or explicitly sync online.
Changing Python, protected dependencies or the kernel component may invalidate
the old lock or bundle. Resolve and review a supported pair on the new release,
then export a new bundle for that release and target. NE06 qualifies alpha.12 to
alpha.13 with unchanged Python/dependency inventories; it does not establish
cross-Python, engine-major or cross-target upgrade compatibility.

Do not delete retained program releases while environments reference their
interpreters. `env gc` collects only known inactive, unleased generations in the
current project/worktree; it does not remove program releases. Keep the source
release and backup needed for rollback. Existing kernels hold leases until
shutdown/recovery has reaped their processes.

After a crash, inspect the intended declaration pair and `env status`. Interrupted
preparations fail; recovery does not promote an on-disk worker report. A crash
between declaration publication and activation can leave the new complete pair
with the previous active environment. The old pair is retained under
`notebooks/.environment-<operation-id>`. Review both, restore the intended pair,
then explicitly sync using a new request key. Never delete the platform upgrade
journal to force startup.

Missing locks require restoring both reviewed files. A drifted generation must
be rebuilt, not edited in place. Package failures retain private diagnostics in
the data root; inspect the matching operation's `worker.log` locally. These logs
are not part of the public qualification report.

## Supported matrix and limits

| Item | Qualified scope |
| --- | --- |
| Linux | x86_64, glibc 2.39+, Ubuntu 24.04 baseline |
| macOS | Apple Silicon, macOS 15+ |
| Python | Bundled CPython 3.12.13; no system or user site-packages |
| Packages | Compatible PyPI registry wheels; pure Python and native wheels |
| Examples | humanize 4.13.0/4.14.0, xxhash 3.5.0, boltons 24.1.0 |
| Builds | User sdists, editable installs, VCS/URL/path sources and custom indexes unsupported |
| Protected closure | Bundled ipykernel, Spark Connect and their pinned dependencies |
| Browser | Chromium qualification; JavaScript outputs/widgets remain disabled |

A wheel's tags and Python requirements must match the current target. A native
wheel can still require unavailable external libraries; successful installation
does not qualify every package's behavior. Supabricks validates the kernel
closure; test your imported dependencies in the intended notebook.

Preparation enforces one writer, eight queued requests, 512 MiB aggregate worker
RSS, 1 GiB per generation, 2 GiB each for resolver/artifact caches and 3 GiB of
operation scratch. Admission needs 2 GiB free with a 512 MiB reserve. Package
transactions have a 180-second deadline; qualified base preparation has 60
seconds. Bundle/download transfer is capped at 512 MiB. These are enforced
limits, not promises of constant memory, disk use or latency. Release evidence
records measured preparation/start times, allocated generation size and bundle
size on its runner.

See the [NE06 qualification contract](../architecture/ne06-environment-qualification.md)
for exact archive identities, test scope and evidence retrieval.
