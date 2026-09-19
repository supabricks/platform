# Runnable sales project

This source template demonstrates ordered migrations and approved CSV ingestion.
Before packing, copy the matching release's `python/notebooks/environments/base/pyproject.toml`
and `uv.lock` into `notebooks/environment/`. Its checked-in minimal declarations
are inspection fixtures, not a qualified notebook environment. Native release
assembly replaces them with the target release's qualified base pair.

For a transferable dependency closure, export the prepared environment with
`env export-bundle ABSOLUTE_PATH --offline --wait`, copy that ZIP under
`dependencies/`, include it in `package.include`, and declare it under
`[environments.notebook.bundles]` using `linux-x86_64` or `macos-arm64` as the key.
Each bundle must contain exactly the same declaration pair as this project.
The matching release already includes the base wheels; custom environments must
supply a verified target bundle or have their locked wheels available offline.

After unpacking, explicitly create a deployment, review `project plan`, and
apply that plan. The fixture creates `public.sales` with two rows (total 30).
Each migration commits separately; retrying checks its PostgreSQL receipt.
Use `analytics open --branch main --wait` to publish a snapshot and then run
`analytics sql --branch main --sql 'SELECT sum(amount) FROM public.sales'`.
Open the installed environment worktree in the console to execute the notebook.
No notebook cell runs during apply.

See `PROJECT-OFFLINE.md` in the release for the complete packaging workflow.
