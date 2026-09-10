# Notebook implementation review: N00–N06

Reviewed 2026-09-10 at `cb1f4f7` on `feat/extract-console`, including N06.
This is a review of the implementation and its tests, not a new qualification
of the complete installed product. No runtime implementation was changed.

The notebook product is not complete. N00–N02 establish a useful architecture,
component probe and owned runtime, but the product editor does not implement
the runtime contract. Several independent failures prevent execution and safe
save/reopen. Prior completion claims based on the frontend build were too strong.

## Findings

P1 means fix before calling the notebook workflow usable or shipping this
integration. P2 means a concrete correctness or completeness issue to resolve
before qualifying the preview. References below are relative to this review.

### 1. P1 — Start retains the stopped generation; subsequent controls fail

[notebook.tsx:32–59](../../console/src/notebook.tsx) discards the start response
and stores the generation from create (0). [runtime.rs:181–217](../../crates/local/src/notebooks/runtime.rs)
increments it to 1 and returns `starting`, not `ready`. Ticket issuance requires
both the current generation and a live context. Interrupt, restart and shutdown
also reject stale generations. The UI never polls status, ignores lifecycle
responses, and presents a session object as proof that the kernel is running.

Reproduced with the built console in Chromium and mocked responses matching
N02: create returns 0/stopped, start returns 1/starting; ticket and shutdown are
sent with generation 0. Both errors become unhandled promises while the running
badge remains visible. This is a contract test, not a real-kernel browser test.

Use typed lifecycle responses and an explicit state machine. Track generation
from each response, wait for readiness, reconcile restart generations, report
terminal states and preserve a handle through uncertain/failed requests. Disable
duplicate lifecycle submissions and close sockets on disposal.

### 2. P1 — N06 cannot negotiate or speak the implemented channel protocol

[notebook.tsx:39–51,65](../../console/src/notebook.tsx) opens `/kernel`, requests
only the authorization subprotocol, and sends JSON text. The server dispatches
upgrades only on `/channels` and requires both the Jupyter v1 protocol and
the authorization subprotocol. `/kernel` is an authenticated HTTP status route.
See [server.rs:202–209](../../crates/local/src/console/server.rs) and
[server/notebooks.rs:239–259](../../crates/local/src/console/server/notebooks.rs).

Even after fixing path and negotiation, [server.py:171–189](../../python/notebooks/server.py)
requires v1 binary offset framing; JSON text is rejected. Incoming binary frames
are fed to `JSON.parse` and the resulting errors are silently ignored by the UI.
These are independent blockers after fixing finding 1.

Use the qualified Jupyter service/widget adapter with a narrow transport bridge,
or a correctly tested adapter for this exact binary contract. Reuse the existing
runtime harness as protocol evidence, not as a substitute for product tests.

### 3. P1 — Contents operations escape the project notebook root via symlinks

[files.rs:191–217](../../crates/local/src/notebooks/files.rs) joins a lexically
validated relative path to `notebooks/`. Load checks only the final component;
save follows parent directories. Neither rejects a symlink root or a symlink
ancestor. List's separate symlink checks do not protect direct get/save requests.

Reproduced against the actual module in a temporary fixture: set
`project/notebooks/link -> outside`, then save/get `link/escape.ipynb`. Both
succeed, and the file is created outside the notebook root. This violates the
contents boundary; it is distinct from explicitly running trusted Python.

Anchor operations to the permitted root, reject symlinks at every component and
protect against replacement races through descriptor-relative operations or an
equivalent robust approach. Qualify both supported OS targets.

### 4. P1 — Browser timestamp rounding breaks ordinary subsequent saves

[files.rs:197,207–220](../../crates/local/src/notebooks/files.rs) emits a numeric
nanosecond timestamp and compares it exactly on save. [notebook.tsx:10,23,29–30](../../console/src/notebook.tsx)
stores it as a JavaScript number. Current epoch nanoseconds exceed the exact
integer range of that type.

Reproduced: `1789018651909997535` becomes `1789018651909997600` after browser JSON
round-trip. An unchanged file is rejected as modified. Reloading repeats the
precision loss, and the rejected save promise is not displayed in the editor.

Use an opaque string revision, preferably tied to document content. Preserve
the distinction between creation and conditional replacement; do not solve this
by disabling conflict checks.

### 5. P1 — Creating a notebook can overwrite an existing file without checking

[files.rs:200–217](../../crates/local/src/notebooks/files.rs) checks the existing
version only when `expected_mtime_ns` is present. A new notebook sends no version.
Entering an existing name therefore replaces its contents without confirmation
or a conflict. This was reproduced against the actual module.

Make absent expected revision mean create-only, with an atomic no-replace
operation. Require a matching revision for an existing document.

### 6. P1 — New files are not valid standard notebooks

[notebook.tsx:4,65](../../console/src/notebook.tsx) creates code cells without
`outputs` or `execution_count`, and declares nbformat minor 5 without cell IDs.
The server validator does not enforce the code-cell schema. The installed
`nbformat.validate` rejects the generated document with
`'outputs' is a required property` and warns about missing IDs.

In addition, the server accepts standard string-array cell sources, while the
browser model assumes a string and passes the value directly to the textarea
and execute request. Normalize sources on load and construct valid cells through
the qualified notebook model. Validate round-trip with the standard reader.

### 7. P1 — Opening or creating another document silently drops unsaved edits

[notebook.tsx:21–30,62–65](../../console/src/notebook.tsx) replaces the active
document without dirty tracking or a save/discard decision. Save, open and
lifecycle button handlers also discard rejected promises. A failed save leaves
the editor data in memory, but the next file click discards it without warning.
Overlapping open/save requests can apply a late response to a newer selection.

Track document identity, dirty state and in-flight operations, retain edits on
failure, surface conflicts and guard document switches. Provide the planned
download/recovery path. Scope asynchronous responses to the initiating document.

### 8. P2 — Output correlation and execution state are incorrect

[notebook.tsx:45–50,65](../../console/src/notebook.tsx) expects output metadata
to echo `supabricks_cell`. Jupyter output identifies its originating request
through `parent_header.msg_id`; arbitrary request metadata is not automatically
copied. A local check with the installed Jupyter `Session` produced empty output
metadata and a correctly matching parent ID.

After fixing transport, normal output still will not be assigned to a cell.
Repeated stream chunks would also replace rather than append, and the output
map is not cleared or scoped when switching notebooks. Socket existence enables
Run even before OPEN or after close. There is no pending execution map,
reply/idle completion tracking, serialized run-all, or interrupted/failed cell
state. Backend concurrency rejection does not substitute for this UI behavior.

Use request IDs and stable document/cell identities, bounded output accumulation,
proper clear/display/error handling and explicit execution completion. Never
automatically replay a request after disconnection.

### 9. P2 — Malformed source arrays overflow validation

[files.rs:84–94](../../crates/local/src/notebooks/files.rs) maps invalid source
members to `usize::MAX` then sums the array. With source `[null, "x"]`, the actual
module panics in a debug build and accepts the malformed document in an optimized
build when addition wraps. The handler runs synchronously in the daemon.

Reject non-string members directly and use checked, bounded accumulation. Do
not use an overflow-prone sentinel as validation.

### 10. P2 — Ordinary companion files break notebook listing

[files.rs:174–183](../../crates/local/src/notebooks/files.rs) calls
`relative_path` on every regular file and propagates the error for a non-ipynb
file. Adding `notebooks/README.md` makes the entire list fail; reproduced locally.
A leftover temporary file after failed persistence can trigger the same issue.
Save does not clean up its temporary file on an intermediate write/rename error.

Filter eligible notebook files, separately enforce path safety and clean up
abandoned temporary writes. Also bound directory traversal and reads: `load`
currently reads the entire file before checking its 8 MiB limit, and list/read
operate synchronously in the sole daemon writer.

### 11. P1 — Release tests bypass the product notebook workflow

[runtime.mjs:45–128](../../e2e/native/notebooks/frontend/runtime.mjs) asserts
`capabilities.notebooks === false`, drives lifecycle HTTP requests directly and
injects its own correct binary client through `page.evaluate`. It does not click
the product Start/Run/Save controls. The standard console qualification harness
has no notebook scenario. Thus green release checks do not establish N03–N06
editor functionality; findings 1 and 2 coexist with passing runtime tests.

Keep these valuable backend fault tests. Add separate tests through the actual
packaged notebook UI for Python and Sail execution, save/reopen, repeated saves,
restart, disconnect, error output and conflicts. Assert no unhandled page errors.
Run that workflow on both installed native targets before claiming qualification.

## Plan and architecture assessment

The original plan and subsequent PR titles no longer use the same slice names:

| Original milestone | State at the reviewed commit |
| --- | --- |
| N00: architecture and plan | Present; later completion/evidence mapping is stale |
| N01: component qualification | Isolated probe and qualification records present; selects upstream JupyterLab widget |
| N02: owned runtime and transport | Substantial implementation, explicit protocol and native fault harness present; this review is not a fresh full runtime qualification |
| N03: embedded notebook and persistence | Partial and broken by the findings above |
| N04: branch/snapshot/session workflow | Still missing from product UI; PR called N04 added basic file editing instead |
| N05: installed preview qualification | Runtime coverage exists; complete product workflow is not qualified; PR called N05 added lifecycle buttons instead |
| N06 | Not in the original plan; PR adds the broken transport integration reviewed above |

N01's selected JupyterLab widget remains in the isolated probe. The shipped
console depends only on React and implements a custom textarea editor. That
departs from the documented decision to use upstream components and avoid a
custom notebook editor/runtime. The console eagerly imports it and shows its
navigation even though the daemon advertises `notebooks: false`.

The UI chooses the default branch (or first branch), exposes no explicit
notebook branch choice, displays no epoch or source age, and persists no binding
provenance. It has no refresh/rebind workflow or live-handle reconnect path on
reload. Markdown creation/rendering, rename/download, run-all and qualified rich
MIME rendering are also absent. These remain acceptance gaps rather than
completed work hidden behind another label.

The N02 ownership architecture is worth retaining: daemon-owned process identity,
fixed Spark bootstrap, shared analytical admission, scoped short-lived channel
tickets, generation fencing, output limits, no automatic replay and recovery
integration. The immediate problem is making the product use those contracts
correctly and closing the newer file-service holes.

## Evidence collected for this review

- Read the architecture, original plan, N01 decision, N02 runtime/bridge/Python,
  file persistence module, product UI and release qualification harnesses.
- Rebuilt the console at the same N06 commit: TypeScript and Vite pass despite
  the behavioral bugs.
- Ran a Chromium check of the actual built console with mocked lifecycle
  responses matching the inspected backend; reproduced stale ticket/shutdown
  generations, unhandled rejections and a false running badge.
- Compiled the actual `files.rs` by path in a temporary harness (only the store
  error type was substituted) and reproduced symlink escape, false mtime
  conflict, unchecked overwrite, companion-file list failure and source-array
  overflow in debug/release. Its existing three unit tests still pass.
- Checked the generated document and Jupyter reply metadata/framing with the
  installed N02 Python packages.
- Did not rerun the entire Linux/macOS installed suite or claim fresh proof of
  crash/recovery/memory behavior. No live project data or daemon was modified.

Temporary reproduction files are in
`/tmp/supabricks-notebook-review.tUmkmx`; the source and build artifacts examined
remain on the N06/extraction worktrees.

## Recommended repair order

1. Fix contents containment, conditional writes, revision encoding and nbformat
   validation in platform, with targeted regression tests.
2. Carry the latest UI history into the standalone console, but implement its
   connection adapter against typed N02 responses and the qualified JupyterLab
   component. Add real product lifecycle and execution tests immediately.
3. Finish dirty/save/conflict handling, explicit branch/epoch binding, run-all,
   outputs and refresh/rebind.
4. Qualify the complete installed browser walkthrough on both targets; update
   the plan and capability flag from that evidence.

The extraction can proceed, but extraction itself does not resolve these bugs.
N06 should not be treated as a working execution feature in its current form.
