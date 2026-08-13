# Review 001: implementation gaps

Status: addressed 2026-08-13 (all P0/P1 fixed; P2 items remain documented debt — see Resolution at end)  
Date: 2026-08-13  
Scope: `platform/` operator, chart, installer, UI, e2e, and handbook contract

## Reference-grade bar

For this prototype, "reference grade" means:

- A clean engineer can clone, build, install, and run the system from docs.
- The documented contract matches source behavior.
- Core data safety promises are conservative under failure.
- Known M2 deferrals are explicit and do not mask current-contract bugs.
- The test suite exercises happy paths, negative paths, and recovery paths for
  every current promise.

This review does not expand scope into gateway, TLS, IAM, HA, billing,
multi-cell, density-500, or Postgres fork work. Those remain deliberate M2+
items per `docs/handbook/backlog.md`.

## Verification performed

Local checks passed:

```sh
cd platform
cargo test --locked
cargo run --locked --bin crdgen | diff -u chart/crds/sspc-crds.yaml -
helm lint chart
cd ui && npm run build
cd .. && docker build -q -t sspc-operator:review .
```

Notes:

- `npm run build` succeeded but deleted the tracked
  `platform/ui/dist/.gitkeep`; it was restored after the check.
- The existing `sspc` kind cluster was already populated with user-created
  suspended databases and branches, so `e2e/run.sh` and `e2e/chaos.sh` were
  not run in this pass.

## P0 findings

### P0-1: Branch-at-head can silently miss parent writes under ingestion lag

The current branch-at-head path waits up to 20 seconds for pageserver ingestion
to catch up to the parent flush LSN. If it does not catch up, the reconciler
logs that it is "branching anyway" and proceeds:

- `platform/crates/operator/src/reconcile.rs`, around the ingestion wait in
  `apply_branch`.
- `docs/handbook/architecture.md` describes the behavior as fail-closed.

Impact: a branch can be created from an ingested LSN older than the parent's
current flushed WAL, which violates the core branch-completeness promise.

Reference-grade fix:

- Never create a head branch until `last_record_lsn >= parent flush LSN`.
- On timeout, requeue with a status/message that says ingestion is lagging.
- Add an e2e or controllable unit/integration test that proves timeout does not
  create the branch.

Exit criteria:

- A sustained ingestion-lag case cannot produce a branch missing committed
  parent rows.
- Docs and log text match the implemented behavior.

### P0-2: Branch cleanup can leak cell-side timelines if status was not written

Branch timeline cleanup depends on `status.tenantId`. Status patch failures are
logged and ignored, so a branch can create its cell-side timeline and then be
deleted before status records the tenant. In that case Kubernetes children are
garbage-collected, but the storage-controller timeline cleanup can be skipped.

Relevant code:

- `patch_status` ignores patch failures in `platform/crates/operator/src/reconcile.rs`.
- `cleanup_branch` only calls `delete_timeline` when `br.status.tenant_id`
  exists.

Impact: storage leaks and stale timelines after delete races or status API
failures. This violates the cleanup/idempotency contract.

Reference-grade fix options:

- Treat status persistence after cell-side creation as required and return an
  error if it fails.
- Or derive/find the tenant during branch cleanup from the owning Database
  when status is missing.
- Prefer storing enough cleanup identity in a place that finalizer cleanup can
  rely on, or make cleanup discovery robust.

Exit criteria:

- Deleting a branch whose status is missing still deletes the cell-side
  timeline.
- Add a test for cleanup when `status.tenantId` is absent.

## P1 findings

### P1-1: Idle detection misses short-lived activity

The design says M1 idle detection uses current client backend count plus a
monotonic activity signal such as `pg_stat_database` xact delta. The
implementation only counts currently connected client backends.

Impact: periodic short-lived clients can be classified as idle between polls,
leading to premature suspend.

Reference-grade fix:

- Track transaction/activity deltas between lifecycle ticks, excluding the
  operator and `compute_ctl` sessions.
- Add tests or an e2e case with short periodic queries over a suspend window.

Exit criteria:

- Short query traffic keeps an endpoint active even if no client is connected
  exactly when the lifecycle poll runs.

### P1-2: Credential lookup silently falls back to legacy shared password

`endpoint_password()` falls back to `SSPC_PG_PASSWORD` on Secret read failures
or malformed Secret data. That was useful for pre-H3 estates, but for H3-era
endpoints it can produce a URI or internal connection attempt with the wrong
password instead of surfacing a retriable platform error.

Impact: confusing connection failures and possible masking of Kubernetes Secret
or RBAC problems.

Reference-grade fix:

- Keep a controlled migration path for legacy endpoints, but make missing or
  unreadable H3 credentials an error.
- Return structured MCP errors from `get_connection` when the credential cannot
  be read.

Exit criteria:

- Secret read failure cannot silently produce a stale/shared-password URI.

### P1-3: Documentation and source disagree on the MCP contract

Observed drift:

- `platform/README.md` says the MCP facade has 9 tools and requires a bearer
  token.
- Source defaults to open mode unless `SSPC_MCP_REQUIRE_TOKEN=true`.
- Source implements the GET/SSE leg.
- Source exposes 14 callable tools.
- `docs/handbook/architecture.md` says 15 tools.
- `platform/install/up.sh` still contains comments describing POST-only MCP.

Impact: new engineers and client authors cannot tell which contract is real.

Reference-grade fix:

- Update `platform/README.md`, `docs/handbook/architecture.md`, and installer
  comments to match the current source.
- Add a generated or checked command that counts the snapshot tools so docs do
  not drift silently.

Exit criteria:

- All user-facing docs agree on auth default, GET/SSE support, and exact tool
  count.

### P1-4: UI build deletes the tracked RustEmbed placeholder

`ui/dist/.gitkeep` is tracked so `cargo test` and `cargo check` work in a
clean clone with RustEmbed. Running `npm run build` clears `dist` and deletes
that tracked file.

Impact: a normal local UI build dirties the worktree and can break the
documented RustEmbed invariant after cleanup.

Reference-grade fix:

- Add a postbuild step such as `touch dist/.gitkeep`.
- Or move the RustEmbed development placeholder to a path not deleted by Vite.

Exit criteria:

- `npm run build` leaves `git status --short` clean when source is otherwise
  unchanged.

### P1-5: Kubernetes hardening is below reference-grade even for a prototype

The chart uses a nonroot runtime image for the operator, but the deployed pods
do not declare pod/container `securityContext`, `allowPrivilegeEscalation:
false`, dropped capabilities, or read-only root filesystems where feasible.
The operator also has no readiness or liveness probe.

Impact: acceptable for early M1, but not a reference-grade handoff. New
engineers have no explicit security/runtime baseline to preserve.

Reference-grade fix:

- Add security contexts to the operator and simple platform pods where image
  behavior allows it.
- Add operator readiness/liveness probes.
- Document any image that cannot run with the stricter posture.

Exit criteria:

- The rendered chart has an explicit security baseline for every workload.

### P1-6: Event/audit behavior is too weak for debugging retry loops

Events are create-only with timestamp names, which is already listed as known
debt. In addition, failed event creation is logged and ignored.

Impact: noisy retries can hide the useful signal, and audit/event delivery is
best-effort despite being part of the UI and runbook experience.

Reference-grade fix:

- Keep best-effort Events if desired, but clarify this is operational signal,
  not durable audit.
- Add higher-signal status messages for long retry loops.
- Consider EventSeries semantics later.

Exit criteria:

- A long-running retry loop is diagnosable from CR status plus logs without
  relying on successful Event creation.

## P2 / known prototype debt to keep explicit

These are acceptable as long as they stay documented and do not masquerade as
finished product behavior:

- `notify-sink` absorbs storage-controller compute notifications.
- The operator is a single writer with no leader election.
- Bad syntactically-valid LSNs can retry forever instead of terminal `Failed`.
- ConfigMap changes do not reach running computes until wake.
- Metrics are in operator memory only.
- NodePort block is a hard 20-endpoint cap.
- Docker build lacks a cargo dependency layer.
- Restore-from-bucket-only is unproven.
- `safekeeper.replicas` is not a working HA toggle.
- CI uses `kubectl exec` rather than host-side NodePort psql.

## Resolution (2026-08-13)

All P0 and P1 findings fixed in one PR; each exit criterion has a verifying
test or a doc that now matches source.

- **P0-1**: ingestion-wait timeout now HOLDS the branch — `status.message`
  set ("ingestion lagging…"), requeue with a fresh deadline; the
  branching-anyway path is gone. Handbook/runbook wording updated to match.
- **P0-2**: `cleanup_branch` derives the tenant from the owning Database
  (status, else `derive_id(uid)`) when branch status is missing. New e2e
  step proves it deterministically: operator scaled to 0, `status.tenantId`
  stripped, delete issued, operator restarted → timeline verified gone from
  the storage controller.
- **P1-1**: idle detection adds the monotonic `pg_stat_database.sessions`
  delta (baseline 1 = the poll's own session) alongside the backend count.
  New e2e step: short-lived clients every 7s over a 20s suspend window keep
  the endpoint Active.
- **P1-2**: `endpoint_password()` returns a Result; missing/unreadable
  credentials surface as structured retriable MCP errors. The
  `SSPC_PG_PASSWORD` fallback and env are removed (backlog debt #3 executed).
- **P1-3**: README, architecture.md, and installer comments now state: open
  mode by default (`SSPC_MCP_REQUIRE_TOKEN=true` for bearer), GET/SSE leg
  implemented, 14 tools — the count is pinned by a unit test so docs fail
  loudly instead of drifting.
- **P1-4**: `npm run build` recreates `dist/.gitkeep` (postbuild touch);
  worktree stays clean.
- **P1-5**: operator + notify-sink run fully hardened (runAsNonRoot, no
  privilege escalation, all caps dropped, read-only root, seccomp
  RuntimeDefault); operator gains readiness/liveness probes on the MCP
  listener. Stock Neon/postgres/minio images documented as the explicit
  exception in `values.yaml`.
- **P1-6**: Events documented as best-effort operational signal, not audit;
  the long-retry loops (ingestion lag, flush-unreadable) now write
  `status.message` so they are diagnosable without Events.

**Found while verifying (chaos drill 3): a wake did not reset the idle
clock.** Stale `status.lastActivity` from before a suspend outvoted the
fresh pod's start time, so a woken endpoint could be re-suspended on the
first lifecycle tick after waking — out from under the client that woke it
(observed as "data lost" when the reader raced the tear-down). Fixed:
idle-since is `max(lastActivity, pod startTime)`, guaranteeing every wake a
full `suspendAfter` window. This race predates the review; the reboot drill
realigning the tick loop is what exposed it.

Verification after all fixes: `cargo test` 14/14 (tool-count pin added),
`helm lint` clean, `npm run build` leaves the worktree clean, e2e gate PASS
194s (16 steps, incl. the two new review proofs), chaos PASS 138s.

P2 list unchanged and still tracked in `docs/handbook/backlog.md`.

## Recommended next documents

1. `002-data-safety-review.md`  
   Branch correctness, timeline cleanup, suspend/wake durability, restore, and
   destructive operation guards.

2. `003-api-contract-review.md`  
   MCP schema, error semantics, auth modes, UI assumptions, client compatibility,
   and documentation alignment.

3. `004-kubernetes-hardening-review.md`  
   RBAC, security contexts, probes, resource policy, chart values, upgrades, and
   failure behavior.

4. `005-test-matrix.md`  
   Existing tests, missing negative cases, race tests, chaos gaps, restore tests,
   and CI expansion.

