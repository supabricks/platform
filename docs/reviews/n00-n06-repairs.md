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
The updated fixture passes all 12 local browser checks; fresh macOS CI is pending.

The same CI run's macOS ingestion assertions passed, but the job failed when
`hdiutil detach` reported the disposable pressure volume busy. The test now
explicitly closes its pressure-volume SQLite connection; workflow cleanup
retries normal detach before forced detach of that fixture only. Persistent
cleanup failures remain failures, and qualification failures retain a nonzero
exit status. Shell tests exercised each cleanup outcome.

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
