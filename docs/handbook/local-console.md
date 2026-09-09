# Open the local console

The alpha.8 distribution includes a browser console for project identity,
runtime readiness and branch inventory. It needs no Node, system Python, cloud
account or frontend development server on the target machine. The initial UI
includes the overview and [PostgreSQL database workspace](database-workspace.md):
branch controls, catalog browsing, SQL, explicitly saved queries and
[browser CSV/TSV imports](browser-imports.md).

From an existing application project containing `supabricks.toml`:

```sh
supabricks console
```

The command discovers the project, validates console assets, runs the existing
`up` readiness path and requests a project-bound loopback console. It asks the OS
to open its default browser and prints one JSON launch result. If browser opening
is unavailable, open the returned URL yourself within 60 seconds. On a headless
machine, use `--no-open` to skip the OS launch request:

```sh
supabricks console --project /absolute/application --no-open
```

The URL includes a one-use secret in its fragment. Keep it private. A used or
expired link cannot create another browser session; rerun `console` for a new
link. Ordinary page reload uses the existing session. Sessions expire after eight
hours or sign-out; runtime restart revokes them. The page removes the launch
fragment and does not persist credentials, SQL or results in browser storage.

For a new project, initialize it explicitly and create a first database:

```sh
supabricks init my-app
supabricks console
# In a second terminal in the same project:
supabricks database create main --wait
supabricks branch create experiment --from main --wait
```

The overview refreshes every five seconds while visible, or with Refresh. It
includes explicit project/data paths in its copyable first-database command, so
that command can be run from another terminal directory. It
shows each branch's desired state, parent, revision and identity. Desired state
is not proof of an accepting PostgreSQL connection. Click a name to inspect its
full identity; this selection does not change the CLI's worktree selection.
Each launch binds one canonical project/worktree. Use another project terminal
for another console; at most four project consoles run per cell.

Closing the browser leaves the runtime and console bridge running. `Sign out`
revokes that browser session. `supabricks down` stops the cell and all console
bridges. `supabricks console` starts it again without losing branches. If the
project file changes identity, open a fresh console after the old bridge exits.

Chromium is the automated browser qualification target on Linux x86_64 and
macOS arm64. Safari/Firefox and remote forwarding are not yet qualified. macOS's
default browser may be Safari: use `--no-open` and open the returned URL in Chrome
when following the qualified workflow. On Linux, automatic opening requires the
desktop's `xdg-open`; the returned URL works when that helper is absent.

## Source builds and diagnosis

Build the frontend once with Node 20.19+ (CI uses Node 22):

```sh
npm ci --prefix console --no-audit --no-fund
npm run build --prefix console
cargo build --locked -p supabricks-local
```

Initialize the native cell using the existing source-build `up --bundle PATH
--helpers PATH` instructions before running `target/debug/supabricks console`.
Source console assets are discovered at the build checkout's `console/dist`.
Installed releases always use `share/console` relative to their own executable.
Copying only a source binary to another machine is not a packaged installation.

Missing, modified, extra or incompatible assets fail validation. Rebuild source
assets, or reinstall a verified release; do not copy arbitrary files into an
installed release. `installation verify` includes the console inventory. A
frontend/API mismatch requires a matching release, not a page-cache workaround.

When the runtime is unavailable, use `supabricks doctor --project PATH` and the
existing runtime diagnostic. Console process state is in `status`; temporary
workspaces are under the private `data/tmp/console-UUID` directory. Console
authentication and temporary workspaces are omitted from recovery bundles. No
server access log is written, and launch secrets must not be pasted into issues.
