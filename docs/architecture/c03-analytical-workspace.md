# C03 — Analytical workspace

Status: implementation and qualification in progress, 2026-09-13.

C03 completes the analytical console slice from the original
[console and ingestion plan](../plans/console-ingestion-implementation.md).
Platform #42 and console #3 delivered I03 and are merged. N00–N06 and NE01–NE06
already delivered notebooks and managed environments; C03 reuses their A02/A03
publication and Sail session machinery. It does not add another execution engine,
notebook environment, catalog, or database migration. Catalog version remains 10.
The candidate release advances to `v0.1.0-alpha.15`.

## Product contract

The database workspace offers explicit PostgreSQL and Analytics modes. PostgreSQL
queries read the selected live branch; Spark SQL queries read an explicitly opened
Sail session pinned to an immutable Delta epoch. Navigation never changes an active
session's branch or epoch. The two identities remain visible when they differ.

The source panel shows the latest epoch, ordinal, source observation time,
publication time and published table inventory. Snapshot age describes the time
since observation, not whether PostgreSQL changed. There is no live CDC or promise
of cross-engine collation equivalence. JSONB and unsupported table/type features
block the whole refresh under A01's existing rules. Errors retain the old pointer;
the UI never silently skips incompatible application tables.

Publishing after import is an explicit operation. Refresh is polled through its
durable operation ID; the browser remembers that ID across reloads in session
storage, scoped by installation data root and project. Refresh progress uses the
existing export/publication states, with no invented completion percentage. A
refresh continues if the browser closes, and cancellation uses A03's existing
fencing and prior-publication preservation.

Opening a session requires a published snapshot. C03 deliberately keeps initial
publication separate from session admission, so a lost browser does not initiate
an invisible first export. An open session stays pinned across refresh. Users
choose another session or explicitly open the latest epoch. The installation's two
session slots are shared with notebooks and CLI; opening a third surfaces the
existing capacity error. Closing or cancelling one session does not close peers.

Spark SQL is read only and uses A03's existing constraints: 32 KiB SQL, 1–1000
rows (200 default), 256 KiB result budget, and a 100–30000 ms deadline (10000
default). The browser polls admitted work, never replays a query after a transport
error. Cancelling stops the entire session. Results show engine, epoch, query
state, original SQL, typed columns, exact string values, SQL NULL and truncation.
One bounded result can be retained for visual comparison across branches or epochs;
this is a selected-result comparison, not a full database diff. Use ORDER BY for
meaningful row order. Retained results are memory-only historical data.

## Ownership and recovery

`console/src/analytics.tsx` lives in `supabricks/console`; platform pins that source
as its existing submodule and ships the usual asset manifest. The console uses a
typed `analytics` workspace adapter in `crates/local/src/console/analytics.rs`.
HTTP keeps the existing authenticated cookie, CSRF, Origin/Host, project/worktree,
and daemon-generation checks. The sole daemon writer dispatches existing A02/A03
operations. There is no direct browser access to SQLite, Sail endpoints or workers.

Session handles belong to the authenticated browser owner and project/worktree.
Listing reconnects that owner to admitted handles after reload or a lost response.
Other browser owners cannot read, close or cancel them. Publications and refreshes
are project resources, as in the notebook refresh controls. Idempotency keys include
the browser scope; a transport failure is surfaced instead of automatically retried.

The browser sends a heartbeat by listing sessions every two seconds, including
when navigating to another console view. After 120 seconds without an owner's
heartbeat, the daemon requests closure through the existing session supervisor.
Unreliable unload requests are not required. Multiple tabs sharing a cookie share
ownership; the last active tab's loss starts the abandonment interval. A suspended
browser may lose its session and must explicitly open another. The absolute session
TTL remains 15 minutes regardless of heartbeat. Terminal handles expire from the
adapter after ten minutes, with a global 128-handle admission bound. Lists contain
summaries; only an explicitly selected session returns its bounded query result.

Worker death and lease release follow A03/NE06's existing process identity fencing,
including safe Sail workspace aliases. Daemon restart invalidates browser handles
and fences launched readers; it never reconnects to old worker endpoints or replays
SQL. Epochs remain durable. Generic browser diagnostics omit raw worker details;
the session and refresh IDs support explicit CLI diagnosis.

## Implementation and qualification sequence

1. Merge I03, branch C03 from main in a separate worktree, retain the source UI
   split, and reconcile this plan with the completed notebook work.
2. Add the scoped publication/session adapter, capability bit, bounded heartbeats
   and sanitized browser responses without a new catalog migration.
3. Add engine selection, publication progress, pinned session controls, bounded
   Spark SQL results and retained comparisons to the console.
4. Extend real browser qualification: device CSV import → PostgreSQL → publication
   → Sail; mutation → old result stable → refresh → new result; full slots,
   foreign-owner handles, reload, truncation, cancellation, failed refresh,
   abandoned browser cleanup and daemon restart.
5. Run Rust contracts and existing native/browser suites, then qualify the exact
   pinned console and platform source in installed Linux x86_64 and macOS arm64
   archives. Existing notebook/environment/package/recovery gates remain enabled.
   Publish paired PRs with exact results and limits; do not represent a locally
   modified engineering archive as release evidence.

R04 remains the follow-up closure of the combined local product workflow, support
matrix and release/demo handoff. C03 adds its real browser checks to the existing
installed-console release gate; it does not claim that the broader R04 work is
complete. Hosted delivery, Sail source builds, IAM/RBAC and a Unity-style catalog
remain separate workstreams.
