# NE05: notebook environment controls

The notebook console exposes the NE04 environment manager through authenticated
structured workspace actions. The UI lives in `supabricks/console`; platform
pins its tested source commit as the `console` submodule. No new package manager,
Python interpreter or execution protocol is introduced.

## Notebook workflow

Opening the notebook view reads environment status only. **Start kernel** still
prepares the qualified base offline. **Initialize environment** is available when
a user wants to declare packages before starting a kernel.

The environment panel shows Python, declared requirements, recorded installed
versions, current preparation work and errors. Installed versions have separate
selected-kernel and prepared columns. Preparation does not change running Python.
**Use prepared environment** becomes available when the current declaration has a
compatible prepared generation. It explicitly restarts Python, discards variables,
preserves the analytical epoch, and never executes saved cells. Snapshot refresh
and environment adoption remain independent. Saved outputs retain the environment
and snapshot in which they were produced.

**Offline packages only** is enabled initially. Uncheck it explicitly to allow
registry resolution and wheel downloads from PyPI when adding/removing packages or
preparing the locked declaration. Preparation, resolution and cancellation use
durable NE04 operations. Cancelling preserves both the notebook buffer and the
package input. A dependency conflict keeps the last ready generation available.
The protected runtime dependencies cannot be removed through the UI.

The diagnostics section supports importing a reviewed project declaration or
an offline wheel bundle. Declaration paths are relative to the bound project;
bundle paths identify files on the local runtime's machine and require an
absolute canonical parent. Bundle import is always offline. These are local path
controls, not browser file uploads. Existing NE04 path, archive, source, hash,
resource and publication restrictions apply unchanged.

A missing lock disables mutations and explains how to restore the reviewed pair.
After moving a project, prepare its declarations at the new location. File drift
is checked at kernel admission, rather than repeatedly hashing whole environments
while displaying package lists. A rejected old generation requires preparation
and explicit adoption. Unsupported package magics still explain the managed CLI
workflow; the panel supplies the browser equivalent. Notebook packages do not
change dependencies on Sail's execution workers.

## Transport and compatibility

The additive capability `notebook_environment_controls: 1` advertises the browser
bridge and expanded inspect response. It is distinct from NE04's
`notebook_packages`, which only advertised CLI/MCP support. Without the new
capability the UI sends no environment commands and retains baseline notebook
controls and CLI guidance.

`POST /api/workspace` accepts `{action: "environment", command: ...}`. The command
is the existing strict environment enum; it cannot supply a new project binding,
interpreter, shell command or index. The console's Host, Origin, session, CSRF,
daemon-generation and instance-ownership checks apply. The daemon supplies the
bound project/worktree; operation lookups and cancellation remain scoped there.
The HTTP body remains bounded at 60 KB. Environment admission gets the same
six-second daemon control budget as notebook admission.

Inspection adds declaration state, bounded TOML requirements, qualified Python
and target, and scoped generation summaries. Package versions come from validated
preparation records (or the matching qualified base contract for pre-NE05 base
generations). Inspection does not execute Python or resolve dependencies. Ready
status remains subject to complete inventory verification on start/adoption.

Mutations submit a fresh request key plus the inspected manifest and lock hashes.
A conflict requires explicit refresh/retry; the UI never changes the expected
revision and resubmits automatically. A transport interruption triggers a read-only
lookup by the same key. Only the key is retained in browser session storage,
scoped to project/worktree. If lookup is unavailable, further package submissions
are disabled until status is recovered. A key confirmed absent can be explicitly
cleared without executing the original request. Poll responses cannot overwrite
newer command results.

Repeated browser preparation exposed uv retaining another unpacked kernel closure
for every transaction's unique wheel staging path. Package installation now uses
`uv --no-cache pip sync` with `TMPDIR` inside the operation's bounded scratch.
Resolver metadata and the verified artifact cache remain reusable offline. Normal
completion, cancellation and recovery clean the installation scratch after reaping
the owned worker. This does not change dependency resolution or running kernels.

## Validation

Rust tests cover declaration status, missing locks and symlinks, recorded inventory
and scope, drift refusal at admission, and authenticated strict workspace requests.
The real browser harness is `console/scripts/qualify-environments.mjs`; it uses the
packaged console, daemon, managed Python kernels and Sail. It covers opening
without preparation, package version isolation and imports, explicit adoption,
unchanged epoch, resolution failure, offline preparation, cancellation, external
edits, missing locks, package magic guidance, saved provenance/reopen, two projects,
offline bundle import and an older capability response.

The `release-environment-console` job runs on Linux x86_64 and macOS arm64 against
archives installed through the signed localhost installer. The browser is limited
to loopback; the host can access PyPI for the explicit online cases. The existing
offline notebook release job remains separate. Broader clean-host network denial,
upgrade/restore and lifecycle qualification belongs to NE06.
