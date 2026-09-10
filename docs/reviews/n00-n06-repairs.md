# N00–N06 notebook repair record

Updated 2026-09-10. This follows the [review at cb1f4f7](n00-n06-notebooks.md),
which remains a historical description of the broken implementation.

## Findings and repairs

| Finding | Repair | Regression evidence |
| --- | --- | --- |
| 1. Stale generation and false running state | Typed lifecycle results, readiness polling, generation reconciliation, explicit connection state and errors | Actual product start, restart, stop and attach |
| 2. Incompatible channel protocol | `/channels`, both required subprotocols, upstream Jupyter v1 binary serialization | Actual product Python and real Sail execution |
| 3. Symlink escape | Descriptor-relative directory traversal and file operations with `O_NOFOLLOW`; no-replace create/rename | Root, ancestor, leaf and directory-replacement tests |
| 4. Rounded timestamps | Opaque SHA-256 string revisions end to end | Repeated browser saves and console-to-daemon integration |
| 5. Unconditional overwrite | Missing revision means atomic create-only; replacement requires current revision | Existing-file save-copy rejected; stale updates retained in editor |
| 6. Invalid notebooks | Upstream notebook model, valid cell IDs/output fields, normalized string-array sources, bounded server validation | Standard installed Python `nbformat.validate` accepts saved product file |
| 7. Lost edits and unhandled failures | Dirty state/discard guard, serialized operations, editing frozen during load/save, visible errors, download recovery | Conflict, cancelled discard, download and reopen tests |
| 8. Incorrect output handling | Parent message-ID correlation, stream append, execute-reply plus idle completion, serial run-all, explicit disconnect failure | Chunked stdout, dependent cells, interrupt, reload and explicit attach |
| 9. Validation overflow | Reject non-string source members and checked size accumulation | Malformed-array tests |
| 10. Broken/unbounded listing and reads | Filter companion files, bounded traversal and reads, exclusive temporary writes with cleanup | README/temp filtering and sparse oversized-file tests |
| 11. Tests bypass product | Retain runtime fault tests and add browser tests using the shipped editor/buttons | Both harnesses required in Linux/macOS `release-notebooks` jobs |

The integration also closes plan gaps: actual lazy-loaded JupyterLab notebook
and CodeMirror components; Markdown and sanitized table/static-image output;
rename/download; branch choice; saved epoch binding; refresh progress/cancel;
explicit rebind; and opt-in output persistence. Each executed code cell records
its own branch/epoch provenance, so rerunning one cell does not relabel older
cells' outputs. Restart preserves the pinned epoch; latest data requires an
explicit new binding. No raw Python execute endpoint was added.

Notebook payloads now have a dedicated transport budget. Other daemon requests
retain their 64 KiB limit; notebook documents may use the existing 8 MiB policy.
An integration test sends a 2.4 MB document through both transports, saves it
again, rejects a stale update and renames it.

The runtime CSP keeps scripts restricted to local assets. A per-response nonce
allows Jupyter/CodeMirror stylesheet elements; output HTML cannot supply style
attributes or image URLs. Package assembly preserves runtime dependency notices
and exact frontend locks alongside the existing provenance.

## Reproducing validation

```bash
cargo test --locked -p supabricks-core -p supabricks-local
npm ci --prefix console
npm run build --prefix console
node console/scripts/qualify-notebooks.mjs \
  --binary /absolute/path/to/installed/supabricks \
  --report /tmp/product-notebooks.json
```

For a source build, additionally pass `--bundle`, `--helpers`, `--python` and
`--worker` pointing to the qualified engine, helper programs, locked analytics
Python and `python/analytics/export.py`. The harness creates isolated temporary
project/data roots and shuts them down. It does not use the running demo.

Local Linux evidence covers the real editor querying PG-backed Sail, snapshot
refresh isolation, restart retaining the original snapshot, explicit latest
binding, sequential cells, repeated saves, rename, conflict recovery, interrupt,
Markdown, hostile HTML, PNG output and explicit reconnection without replay.
The browser report asserts no unhandled page or CSP errors. The saved document
is validated by the installed Python nbformat reader.

This local test uses the source-built CLI with previously qualified native
components. Its browser is restricted to loopback; the host network is **not**
isolated. It is not proof of the exact signed release or a fresh macOS result.
The native release workflow runs `qualify-all-notebooks.mjs` against the signed,
relocated installation, preserving runtime fault coverage plus the actual
product workflow under the existing network isolation on both targets.

## Remaining qualification boundaries

The first exact-archive CI run at `f689236` passed the installed Linux notebook
workflow and runtime fault checks on both targets. The macOS product fixture
failed before opening the browser: `/private/tmp` expansion made its compute
socket budget 106 bytes, exceeding the runtime's 104-byte limit. An isolated
local reproduction at the same length returned that exact `start_compute`
error. The fixture now uses a shorter prefix, checks the canonical path budget
before launching services, and preserves structured CLI errors in its report.
The updated fixture passes all 12 local browser checks. The subsequent CI run
at `3d51a05` passed both macOS notebook and ingestion release qualification.

The same CI run's macOS ingestion assertions passed, but the job failed when
`hdiutil detach` reported the disposable pressure volume busy. The test now
explicitly closes its pressure-volume SQLite connection; workflow cleanup
retries normal detach before forced detach of that fixture only. Persistent
cleanup failures remain failures, and qualification failures retain a nonzero
exit status. Shell tests exercised each cleanup outcome.

That subsequent run failed the Linux notebook memory-limit check with
`contains an unverified process; recovery stopped`. The kernel was fenced as
`runtime_failure` before the test could observe `memory_limit`. All other jobs
passed. A local Linux regression reproduced the same ownership conflict in
0.08 seconds by sampling a token-bearing shell repeatedly executing itself.
During `exec`, `/proc/PID/environ` can return empty or partial data even though
the PID, birth time, process group and UID remain unchanged. Retrying only
empty reads was insufficient; a partial nonempty read reproduced the failure.
This reproduces a cause of the CI symptom; the original CI diagnostic did not
capture the offending PID or environment read.

Ownership verification now retries a missing token for up to 50 ms, requiring
a complete NUL-terminated token field and unchanged process identity before
acceptance. Disappearance, a zombie or changed identity rejects ownership;
deadline expiry also rejects it. Unrelated live group members remain conflicts,
and conflict diagnostics now identify their PID without exposing environment
contents. Regression tests cover repeated `exec`, absent tokens and tokens
with a matching prefix but a different owner, alongside existing forged-birth
and orphan-recovery tests.

The portable core/local suite passed, and all five process-ownership tests
passed with the final token-boundary check. The local source runtime harness
passed 24 checks, including the failing memory-limit case, real Sail queries,
interrupts, expiry and daemon-crash reconciliation. This source run uses the
previously qualified native components and does not restrict host networking;
its external-launch mode omits the two installed-layout cleanup/backup
assertions. Exact-archive Linux/macOS release qualification subsequently passed
at `27014d4`, including notebooks, console, ingestion and recovery.

The separate N01 component probe at that commit timed out on the first query
after shutdown/start. This probe uses the frozen alpha.8 release, not the new
platform executable. Its restart path waited only for a connected WebSocket;
initial startup already required a kernel-info round trip. A local repetition
of the original restart path stalled on its fourth cycle: the query appeared
in IPython history, while its browser future received neither reply nor idle
callbacks and the connection still reported connected/idle. The CI report did
not contain message-level diagnostics, so that exact wire-level failure cannot
be established retrospectively.

The probe now awaits Jupyter's `kernel.info` readiness promise on all fresh
starts, allowing the client's initial info exchange to finish before sending
another request. A separate explicit info request also stalled during local
stress testing, so the probe uses the upstream initialization promise. It
checks for a new kernel identity and exercises three restart/query cycles.
Kernel-info and query waits are bounded to 60 seconds without replaying query
code. Failure reports are marked failed and identify the active operation;
query timeouts include reply/idle receipt and connection/kernel state, without
code, outputs or credentials. These are component-probe changes; production
notebook runtime qualification remains separate.

The final readiness change passed two complete local probe runs expanded to
six restart/query cycles each (12 cycles total), including admission cleanup,
closed analytical sessions, departed kernel processes and nbformat validation.
A separate real sleeping-query injection failed at the 60-second request
deadline with `busy`, no reply and no idle, instead of leaving a running report
until the global watchdog. JavaScript syntax and the frontend build passed.
These source-adapter runs used the qualified alpha.8/Jupyter components with
unrestricted host networking; fresh packaged Linux/macOS CI remains required.

The expanded original
N03/N04 acceptance matrix (including disk-full injection, child-branch workflow,
full-session admission and expired/deleted saved bindings through the product)
must not be inferred from the smaller local browser suite. Existing backend
fault tests cover several related runtime conditions, not all product flows.

Content revisions detect completed external edits and serialize saves through
the daemon. External editors do not participate in a filesystem transaction:
an unrelated writer modifying a file between the final revision check and
rename can still race a save. Descriptor-relative operations prevent pathname
replacement from redirecting writes to a substituted symlink destination.
