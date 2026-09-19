# PK06 — console project packages

The Project packages workspace in `supabricks/console` uses the platform's
existing source inspection, `.sbproj` verifier, destination binding and durable
plan/apply implementation. The browser does not resolve resources or grant
permissions. The advertised capability is `project_packaging: 1`; older hosts
keep their existing console without this navigation item.

## Workflow

Start a console on an existing local project (`supabricks init` followed by
`supabricks console` for a new cell). Select a `.sbproj` from the device. The
console uploads and verifies it, presents the exact resource graph, dependency
inventories and source file list, then explicitly unpacks it into a new local
source directory. This does not bind a deployment, allocate a database, prepare
an environment or start a kernel.

Create a new local deployment or explicitly attach an existing deployment of
the same definition. Review the platform-generated plan, including logical
branch bindings and ordered initialization/preparation steps, before applying.
Existing branch adoption accepts an explicit logical-name-to-UUID mapping and
uses the same rules as `project plan --adopt`. No name-based adoption is inferred.

The workspace shows public definition, destination deployment, runtime project,
worktree, target, source SHA-256, active installed revision and source drift.
Environment inventory verification is distinguished from successful runtime
preparation. Missing capabilities, native bundles, bindings and preparation
failures surface the platform diagnostic; users fix source declarations or
runtime components and replan. This is a local OS owner workflow, without
Unity Catalog configuration or role editors.

Preview export creates the same deterministic bytes as CLI `project pack`.
Only the declared files travel. Notebook outputs are stripped. Connection
credentials, private saved queries, live database contents and prepared venvs
are excluded. Declared fixture/source contents still require author review;
packaging is not a general secret scanner.

Installed queries and notebooks can be read from the immutable package, copied
to a new source draft without overwriting a file, and reopened through the
installed environment's console. Opening either console or package does not
create a kernel. A new tab preserves query/notebook drafts in the original tab;
package errors and cancellation do not clear those components.

## Transport and ownership

The typed workspace command is `{action: "project", source, command}`.
`source` is either `{kind: "current"}` or `{kind: "imported", id: UUID}`.
Reopening between loopback ports permits a same-site top-level GET of `/`
without an Origin header. API/asset requests keep the existing exact-origin
fences, and cross-site or iframe navigation remains refused. The new tab still
authenticates with a one-use fragment ticket.

HTTP callers cannot supply source, extraction, download or console asset paths.
Imported directories live under
`<data>/console-projects/<host-runtime-project>/<import-UUID>`. Their displayed
worktree is usable by the CLI. They survive console sessions and are included
in cold backups; after restoring to a different location, explicit attachment
is required by PK03. The console never overwrites or deletes imported source.
Manage retained directories through the local filesystem/CLI after use.

Every command is generation- and binding-fenced and verifies that the console
instance owns its launcher binding. Imported selectors are scoped to that
launcher runtime project. The central binding resolver also checks the selected
worktree. No browser-side authorization decision is trusted.

Transfers use private temporary files and ordered 24 KiB chunks through the
same authenticated, CSRF-protected workspace API. Limits are 300 MiB per
archive, two transfers per session, four per daemon, a 64 MiB free-space reserve
and a 15-minute idle lease. Archive expansion/source limits remain PK02/PK05's
limits. Interrupted upload chunks are never replayed automatically; select the
file again. Discard releases the slot, expiry is reclaimed on the next command,
and daemon recovery removes abandoned transfer files. Temporary transfers are
excluded from backup. Downloads read a prepared immutable file; CLI and browser
exports are byte-identical. Archive commands allow 120 seconds for bounded
verification/publication; other console operations retain their control deadline.

`unpack` uses a client-generated destination UUID. A lost-reply retry acknowledges
only the original archive marker and never replaces the destination. Deployment
creation has a stable request key. `apply` carries the exact reviewed plan and a
stable key; stale or modified plans are rejected by PK04. The browser retains the
key on errors, exposes find-by-key recovery and polls only status. It never
resubmits a mutation automatically. The latest durable apply is returned by the
project view for reload/restart recovery. Cancel requests use the existing
retain-only semantics, including initialization receipt reconciliation. Failed
preparation leaves the previous active revision intact.

## Qualification

Rust tests exercise transfer isolation/limits, invalid archives, unpack retry,
no implicit binding, conflicting attachment, stale plans, descriptor/path
rejection and byte-identical export. `console/scripts/projects.mjs` drives real
Chromium and the native runtime through import, source drift, stale plan,
lost reply, conflicting deployment, failed preparation and recovery, export,
asset drafting and CLI/browser identity reopening. It is part of the full
release browser harness; `qualify-projects.mjs` runs it alone for development.
The R04 collector requires this coverage in addition to existing console checks.
Cross-host archive portability qualification remains PK07.
