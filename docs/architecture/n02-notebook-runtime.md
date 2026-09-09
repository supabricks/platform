# N02: owned local notebook runtime

N02 implements the runtime behind the N01-selected Jupyter components. The
ordinary console still advertises `notebooks: false`; N03 adds the editor and
project-file persistence. `notebook_runtime: 1` identifies this internal bridge
contract. There is no notebook execution HTTP endpoint.

## Installation and ownership

The native assembler extends the private Python 3.12 runtime with the exact N01
Jupyter Server/ipykernel lock in `python/notebooks`. Every package from the
analytical lock retains its original version. The analytical environment check
accepts either its original exact environment or the exact platform-applicable
notebook superset. Runtime startup never invokes pip or uv. Builder-only uv is
pinned to 0.11.21. Loader checks cover the added native wheels; the qualified
pyzmq macOS wheel has its unused build-machine libsodium RPATH removed and is
signed again. The release inventory includes the workers and notebook lock.

Creating a handle starts nothing. Starting admits one ordinary A03 session and
lazily launches a private Jupyter server for that worktree. A kernel has exactly
one session and epoch. The installation-wide two-session budget includes CLI
queries and Spark shells. Jupyter servers also have a two-server limit and stop
after 60 seconds with no active kernel.

Jupyter can only launch the fixed `supabricks child` gate. Before that child
executes Python, the daemon checks the pending launch, console-owner lease,
installation generation and ready A03 session, and commits its PID, birth
identity, process token and role to `native_processes`. Its role contains the A03
session ID. The existing A03 close path stops that kernel process group before
releasing the epoch reference. Bootstrap creates `spark` and `epoch`, validates
the epoch identity, and terminates the kernel if initialization fails. Endpoints
and connection credentials exist only in private runtime files and IPC.

Jupyter uses private configuration, runtime, IPython and kernelspec directories;
ambient extensions, native kernelspec fallback, terminals, offline message
buffering and automatic kernel restart are disabled. Its contents root is an
empty private directory. The kernel working directory is the bound project.
Notebook Python runs with the local user's permissions; this is an owned local
runtime, not an untrusted-code sandbox.

## Browser contract

Use the console's existing origin, version header, session cookie and CSRF checks
for `POST /api/workspace` with:

```json
{"action":"notebook","command":{"action":"create","key":"editor-1","target":{"branch":"<branch UUID>","revision":1}}}
```

Commands are `create`, `list`, `status`, `start`, `interrupt`, `restart`, and
`shutdown`. Commands addressing a handle carry its `id` and `generation`;
mutations additionally carry an idempotency `key`. A repeated identical action
returns current status, including after it advanced generation. Reusing a key
with different parameters fails. A fresh action with a stale generation fails.
Handles are scoped to project ID, canonical worktree and console browser session.
A console session owns at most the installation's bounded set of 128 handles;
each handle retains at most 128 action keys.

The only Jupyter bridge routes are:

| Route | Behavior |
| --- | --- |
| `POST /api/notebooks/ticket` | Issue a single-use, 30-second channel ticket for an owned, live handle/generation |
| `GET /api/notebooks/{id}/{generation}/kernel` | Read that kernel's status from the fixed private Jupyter server |
| `GET /api/notebooks/{id}/{generation}/channels` | Upgrade to the standard Jupyter v1 binary WebSocket protocol |

The WebSocket requires the exact console Origin and Host, authenticated cookie,
and two requested subprotocols: `v1.kernel.websocket.jupyter.org` and the ticket's
`sb.auth.*` authorization protocol. Only the Jupyter protocol is negotiated back.
Credentials are never placed in query strings. Paths, upstream host and kernel
identity are constructed by the bridge; arbitrary proxy targets are unavailable.
The bridge checks session and generation throughout an open connection. Logout
cancels existing sockets immediately; expiry and daemon/session loss close them.
Console session exchange optionally accepts `lifetime_seconds` from 10 to 28800
(default 28800), allowing callers to choose a shorter authenticated lifetime.

The console reports its live browser sessions to the daemon every two seconds.
Owners expire after six seconds without a heartbeat. Closing a tab disconnects
its channel; signing out revokes its runtime. Reconnecting to a live kernel does
not resubmit cells. Submitted execute IDs are retained up to 1024 per kernel;
repeated IDs and concurrent execute requests receive ordinary Jupyter error and
idle replies. A full history requires an explicit restart.

## Bounds and failure states

| Resource | Bound |
| --- | --- |
| Jupyter process-group RSS | 512 MiB, sampled by the daemon |
| Kernel process-group RSS | 1 GiB default; caller may lower to 64 MiB |
| Kernel/A03 lifetime | 900 seconds default and maximum; minimum 10 seconds |
| Disconnected idle kernel | 300 seconds default and maximum; minimum 10 seconds |
| Frame and pending browser writes | 2 MiB |
| Output | 1 MiB or 2048 output messages per cell; 10 MiB per kernel |
| Kernel execution | One at a time; at most 32 protocol submissions/second |
| Submitted source | 64 KiB, no interactive stdin |
| Native socket queues | High-water marks of 16 messages; 2 MiB maximum message |
| Console channels | Four; no relay queues; one-second write deadlines |

These are enforcement ceilings, not performance promises. Group RSS can count
shared pages more than once and is sampled, so it can temporarily exceed its
ceiling. Native qualification records observed Jupyter RSS separately for each
target. Protocol frames are validated before the upstream offset decoder can
allocate from an untrusted count. Oversized kernel output terminates at the
sender as well as being checked in the Jupyter monitor and browser bridge.

Interrupt waits for an observed idle state after the interrupt was sent. If that
does not happen within five seconds, the daemon stops the kernel and closes the
A03 session, including Sail, and reports `spark_interrupt_escalated`. Restart is
explicit, advances generation and clears Python variables. Neither restart nor
reconnection replays source. Output/memory/bootstrap failures report `failed`;
process or Spark context loss reports `lost`; lifetime/idle limits report
`expired`. Clients must create or explicitly restart an execution context.

## Recovery and compatibility

Catalog version remains 9. The existing native process records suffice for
ownership, and the existing A03 request JSON marks internal notebook admissions.
No document, executed source, browser token or live notebook handle is restored
from the catalog. Recovery fences Jupyter servers before kernels, verifies owned
process groups have stopped, and closes even waiting notebook A03 admissions.
Ordinary waiting CLI admissions retain their existing recovery behavior.

`down` waits for notebook cleanup before completing analytical shutdown. Backup
uses that barrier and excludes `notebook-work`, which contains only disposable
runtime credentials and control files. Project files remain outside the managed
data backup. Existing R03 upgrade/restore qualification continues to validate
format compatibility; the analytical data-compatibility lock is unchanged.

## Qualification

`e2e/native/notebooks/frontend/runtime.mjs` uses Chromium against an installed
console and real Postgres/Sail data. Its fault helper is test machinery and is
never shipped. `native-release` runs the harness after the signed localhost
installer on both Linux x86_64 and macOS arm64, with external networking denied.
The harness reports individual execution, ownership, resource-bound, revocation,
crash and recovery checks without copying private launch/config files.
