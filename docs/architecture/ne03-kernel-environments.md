# NE03: notebook kernels bound to managed environments

NE03 launches each product notebook kernel in an NE02-managed virtual environment.
Jupyter servers, Sail sessions and ingestion/export workers retain their bundled
service runtime. The default console start path prepares the qualified offline
base automatically on the user's explicit start; opening a notebook alone does
not initialize files, prepare packages or execute code.

## Two independent identities

A handle records an environment generation (declaration hashes, component
contract and materialization inventory) alongside its analytical epoch. Saved
notebook binding and per-cell output provenance retain those identities.
Existing notebooks without environment metadata remain readable and select the
release's offline default on explicit start. Existing saved bindings select their
recorded generation; missing or incompatible generations never silently fall
back to the newest environment.

| Action | Environment | Snapshot | Python variables |
| --- | --- | --- | --- |
| Restart kernel | Retained | Retained | Discarded |
| Use prepared environment | Explicit prepared generation | Retained | Discarded |
| Start on latest snapshot | Retained from saved binding | Latest | Discarded |
| Reconnect | Retained | Retained | Retained if kernel survives |

No action replays cells to restore variables. Earlier cell outputs keep their
own provenance when later cells execute against another environment or epoch.
Removing outputs also removes their output provenance, while preserving the
notebook's selected binding. Saved provenance describes execution history, not a
signature or permission to execute imported code.

## Start and ownership protocol

Explicit start journals the default environment's initialization/preparation
through the existing manager, exposing the pending operation in handle status.
The notebook remains `starting` while preparation proceeds. No A03 session is
admitted until preparation is ready. The existing two-session/server admission,
RSS, lifetime, idle, output, authentication and channel limits remain in force.

Before launch, selection checks canonical project/worktree ownership, private
directory identity, interpreter/installation/component compatibility and the full
materialized file inventory. It rejects symlinks at the generation root, changed
package bytes, missing files, hard links, special files and excessive tree size.
The inventory uses canonical JSON shared between the Python worker and Rust
selector. This contract change requires preparation again for NE02 generations.

The notebook acquires a durable lease before admitting its session. It validates
the selection again before publishing the launch and at the native execution
gate. The kernel uses `-I -B` and the generation's `bin/python`; bootstrap checks
its prefix and disabled user site, opens the admitted Spark session, and reports
both identities. Jupyter requires that report to match the daemon's gate before
exposing a ready channel. The kernel exposes `supabricks_environment` for inspection.

Shutdown and failed bootstrap stop the owned kernel before releasing its lease.
A failure after acquisition but before session creation follows the same cleanup
path. Daemon recovery stops servers and kernels before the environment manager
clears stale leases. Collection cannot delete active or leased generations; two
kernels may continue using old and new generations in the same project.

Changed project declarations mark preparation needed without modifying a live
kernel. `supabricks env prepare` creates a new qualified generation; the console's
**Use prepared environment** action explicitly adopts it while retaining the
snapshot. Arbitrary package editing remains NE04. There is no dependency resolver
or network fallback on the notebook start path.

## Native qualification

The release becomes alpha.10, retaining catalog 10. Runtime materializations are
installation-bound and disposable; source declarations stay in the project.
The native environment gate runs NE02 preparation faults plus `kernels.py` against
an exact Linux/macOS archive with external networking denied. The kernel harness
uses public console authentication and the real Jupyter websocket protocol to
check default offline startup, concurrent project versions, old/new leased
kernels, restart/adoption, saved mixed provenance, drift rejection and unchanged
service imports. Existing console and notebook lifecycle gates continue to run.

Rust tests cover canonical inventory parity, drift and missing-generation
rejection, declaration changes without implicit rebinding, and mixed provenance
save/reopen. Native evidence is pending until the exact-archive jobs complete.
This is local-user process isolation, not a hostile-code or container sandbox.
