# C01: packaged local project overview

C01 implements the first slice of the [console plan](../plans/console-ingestion-implementation.md)
in the alpha.4 distribution. It keeps catalog version 8 and the qualified PG17
engine/analytical dependencies. The browser bridge exposes only session creation,
session inspection, logout and a project/branch/runtime overview. It does not
forward arbitrary local API/MCP methods to HTTP.

## Process and API boundary

`console --project PATH [--no-open]` validates project/assets and invokes the
existing `up` command. The daemon starts one owned bridge per canonical worktree,
with a maximum of four. The stdin-gated launch records PID, birth identity,
generation and process token in the existing native-process table before exec.
The child binds `127.0.0.1:0` itself; there is no select-port/release/rebind race.
A private ready file reports the actual bound port. A repeated launch reuses the
bridge and issues a new independent one-use ticket.

The bridge reads no SQLite files. It uses a scoped `ConsoleOverview` socket
request that revalidates the project file and daemon generation. The response
contains public project identity, worktree, desired branch states/revisions and
bounded runtime readiness, never application connection credentials. SQL/import
capabilities are explicitly false. These private socket additions do not change
the existing public CLI/MCP method contracts.

Separate console ownership recovery runs on daemon bind and fallback shutdown.
Storage-supervisor recovery excludes console children so a console failure does
not restart storage, and storage recovery does not silently reuse or replace a
browser session. Two failed two-second heartbeat cycles make the bridge exit;
every overview request independently checks the generation/binding. `down` fences
the bridge before returning success. Existing native ownership checks refuse
ambiguous/reused PIDs. Backups exclude the temporary console workspace through
the existing `tmp/` exclusion; authentication is never restored.

## Browser authentication and bounds

Launch tickets use 256 random bits, expire after 60 seconds and are consumed once.
At most 32 unused tickets and 32 active eight-hour browser sessions are admitted
per bridge. Sessions live in child memory. Cookies use HttpOnly, SameSite=Strict
and an instance-specific name because ports do not isolate cookies. Logout checks
an independent CSRF secret. No wildcard CORS, token query parameters, arbitrary
filesystem access or shell endpoint is exposed. Fragment secrets are cleared by
the frontend before it sends its session request.

HTTP validates the exact Host and any supplied Origin, rejects cross-site Fetch
Metadata and requires the console-version header on every API request. All POSTs
require an exact Origin; the JSON ticket exchange supplies its own unguessable
authorization, and logout additionally requires CSRF. The page CSP restricts
assets/connections to the same origin, disallows frames and inline script/style,
and sends a no-referrer policy. Local access still trusts the filesystem owner;
this is not multi-user IAM.

Hyper serves HTTP/1 with 16 concurrent connections, no keep-alive, 32 headers,
a 16 KiB parser buffer, a three-second header deadline and an eight-second total
connection deadline. Session JSON is capped at 4 KiB. Daemon reads use a two-second
timeout and the existing 2 MiB socket response ceiling. No user-selected table
data or unbounded query results are loaded by this slice.

## Immutable assets and packaging

`console/` has a locked React/TypeScript/Vite build and no Carbon/operator
dependency. The generated `console.json` binds API version 1 to the exact asset
inventory and SHA-256 hashes. The Rust loader refuses missing/extra/modified
files, symlinks, incompatible API versions and unsupported file types, caps depth
and file sizes, and serves only loaded asset keys. It holds a bounded in-memory
asset snapshot; URL paths never become filesystem reads.

Assembly copies the assets into `share/console`, records the frontend lock and
manifest hashes, includes React/React DOM/Scheduler and generated Vite-loader
notices and inventories every
file in the signed release. Node and browser automation are build/test tools.
All runtime fonts and assets are local; no CDN or first-load package download is
needed. The portable Rust crates retain their no-operator/no-Kubernetes/no-UI-build
dependency gate; static files are packaged separately instead of requiring a
frontend build for `cargo test`.

## Qualification evidence

Portable integration tests run real daemon/bridge processes and exercise one-use
tickets, origins, version negotiation, CSRF/session revocation, payload limits,
asset corruption, daemon replacement, project-identity changes and stopped backup.
The installed browser harness builds no frontend at runtime: it installs the exact
signed localhost archive and exercises empty/populated project views, root/child
branch state, filtering, independent selection, reload, repeated launch, narrow
layout, keyboard refresh, console SIGKILL, full-cell restart and sign-out.
It verifies the installed inventory after browser use.

Separate release-console jobs run Chromium with external networking denied: an
unprivileged Linux user in a loopback-only network namespace, and the existing
macOS Seatbelt qualification profile. JSON reports identify exact release/browser
versions and checks; screenshots contain synthetic fixture data only. PR evidence
must state which jobs actually passed. Safari, Firefox, VS Code and remote-console
access remain unqualified, as do the broader R03 public durability/release gates.
