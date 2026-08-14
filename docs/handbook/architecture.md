# Architecture as built (M1.5)

One kind/Kubernetes cluster, namespace `sspc-cell`, ≤8 platform pods. Two
layers: a **cell** (Neon's stock, unmodified storage engine) and **our
platform** (one Rust binary: reconcilers + lifecycle loop + MCP server + UI).

```
 agent / UI / psql
   │ MCP (JSON-RPC over HTTP :30080)          NodePort per endpoint (:30001-20)
   ▼                                                     │
 sspc-operator ──── Database/Branch/EnrolledDatabase CRs │
   │ reconcile: derive IDs → storcon API → pods/svc/cm/secret
   ▼                                                     ▼
 storage-controller ──► pageserver (GetPage@LSN) ◄── compute pod (compute_ctl + PG16)
   │ (its own tiny PG)        │                          │ WAL
   └── notify-sink (absorbs   └── S3/MinIO           safekeeper ──► S3/MinIO
        compute notifications)         (layers)               (WAL)
```

## The cell (chart/templates/, stock Neon images pinned by digest)

- **pageserver** (StatefulSet): the versioned page repo. Serves GetPage@LSN
  to computes; ingests WAL from safekeepers; tiers layers to S3 (demo MinIO).
  Self-registers with the storage controller via `metadata.json` +
  `control_plane_api` upcall.
- **safekeeper** (StatefulSet, 1 replica in M1): WAL durability quorum-of-one.
- **broker**: pub/sub between safekeepers and pageservers.
- **storage-controller** (+ `controller-pg`): tenant/timeline API we call for
  everything (`/v1/tenant`, `/v1/tenant/{t}/timeline`, passthrough to
  pageserver endpoints). Runs `--dev`. Its `control_plane_url` MUST be set or
  it panics on the first compute notification — **notify-sink** is an
  always-200 busybox that absorbs those until the operator grows a receiver.
- All cell pods: resource requests (Burstable) + PriorityClass `sspc-cell`
  (100300) — above every compute class, so compute can never preempt storage.

## Identity discipline (reconcile.rs)

Tenant/timeline IDs derive deterministically from CR UIDs:
`derive_id(uid, salt)` = first 16 bytes of sha256. Replays, crashes, and
operator restarts converge on the same cell-side resources with zero state
carried between attempts. Child objects (ConfigMap, Service, Pod, Secret)
carry ownerReferences → Kubernetes GC deletes them with the CR; the finalizer
(`sspc.io/cell-cleanup`) handles only cell-side cleanup (tenant/timeline
deletion), and **refuses** while children exist (RFC 014 H1).

## A Database, end to end

1. `create_database` (MCP) server-side-applies a `Database` CR — SSA on the
   name is the idempotency mechanism; duplicate calls converge.
2. Reconciler: create tenant + root timeline via storcon → mint per-endpoint
   credential Secret `sspc-cred-<name>` (RFC 014 H3; md5 hash rendered into
   the compute spec) → spec ConfigMap → NodePort Service (stable hash-picked
   port from 30001–30020, `ports.rs`) → compute Pod.
3. Compute pod = `compute_ctl` as PID 1 (stock compute image), spec pinned to
   tenant+timeline explicitly, JWT auth against the operator-owned Ed25519
   JWKS (Secret `sspc-compute-jwk`), readiness = `pg_isready` exec probe
   (TCP probes race PG startup and pass too early).
4. CU/priority (RFC 011): `cuLimit` (1 CU = 0.1 core) is the pod's CPU limit;
   the request is a priority-weighted fraction (high /5, standard /10, low
   /20) so CFS contention follows priority; PriorityClasses make eviction
   order match. Preempting a compute is safe by architecture: state lives in
   the cell; eviction is a rude suspend.

## Branches

A `Branch` CR = timeline-with-ancestor + its own compute. `parent` (optional)
points at another Branch (branch-of-branch); `at` (optional) is an LSN
(passthrough) or RFC 3339 timestamp (resolved via pageserver
`get_lsn_by_timestamp`). **Branch-at-head ingestion wait**: the pageserver's
ingested LSN lags the parent's flushed WAL, so a head-branch waits (bounded
20s per attempt) until `last_record_lsn >= parent's
pg_current_wal_flush_lsn()`. **Fails closed on every path** (review 001
P0-1): unreadable parent flush LSN → requeue; ingestion still lagging at the
deadline → status message ("ingestion lagging…") + requeue with a fresh
deadline. A head branch is never created below the parent's flushed head.
Skipped for historical `at` points.

## Lifecycle (lifecycle.rs, 15s tick)

- **Idle detection**: SQL poll per active endpoint with two signals (review
  001 P1-1): connected client backends — excluding `application_name` =
  `sspc-operator` and `compute_ctl%` (the compute_ctl monitor holds a
  permanent session; without the exclusion nothing ever suspends) — plus the
  monotonic `pg_stat_database.sessions` delta, so short-lived clients that
  connect and leave *between* polls still count as activity (baseline is 1:
  the poll's own session). M1-only; the M2 gateway owns activity truth.
- **Suspend** (idle > `suspendAfterSeconds`): mint admin JWT → POST
  `/terminate` on compute_ctl :3080 → record returned flush LSN in status →
  delete pod. Service and ConfigMap stay (sticky port).
- **Wake**: `get_connection` stamps annotation `sspc.io/wake-requested-at`;
  reconciler recreates the pod (always fresh — reusing stale compute state is
  a proven crash loop). Wake wins when the annotation is newer than
  `suspendedAt` (RFC 3339 strings compare chronologically; monotonic, no
  annotation clearing).
- **TTL reaper**: `creationTimestamp + ttlSeconds` past → delete CR + post
  `TTLExpired` Event.
- **Enrolled health**: read-only SQL probe per EnrolledDatabase (version,
  db count, size) — observe and advise, never operate (RFC 010).
- **Metrics**: kubelet Summary API via node proxy (no metrics-server);
  40-sample ring per endpoint in operator memory (10 min at 15s). Feeds
  `get_metrics` and the `get_cu_ledger` oversubscription arithmetic.

## MCP + UI (mcp.rs)

Hand-rolled streamable-HTTP JSON-RPC (POST + idle GET/SSE keep-alive leg —
third-party MCP client requires the GET leg; Claude Code tolerates its absence). 14 tools
(count pinned by a unit test; schema snapshot-tested in `mcp-tools.json`, so
contract drift fails CI), each a thin verb over the CR model — the
reconcilers are the single implementation of behavior. Every tool also
declares an **outputSchema** (result contract, snapshot-pinned). Input is
validated synchronously at the boundary (review 003): names normalize to
lowercase (the canonical name is echoed back), numeric bounds mirror the CRD
schema (`suspend_after_seconds: 0` = never suspend), and enum values are
strict — never silently coerced. Errors come in three layers: HTTP
(401/parse-error envelope), JSON-RPC (`error` envelope), and tool-level
`{reason, retriable, suggested_action}` — the layer agents act on. The
GET/SSE leg is keep-alive only: no session model or server notifications.
Kubernetes Events are best-effort operational signal (create-only, failures
logged and dropped), NOT a durable audit trail — long retry loops surface
through CR `status.message` instead. Auth: open
mode by default (install binds host ports to 127.0.0.1; real IAM is RFC 008),
`SSPC_MCP_REQUIRE_TOKEN=true` for bearer mode. The UI (Carbon, RFC 013) is
rust-embedded into the binary and speaks only MCP tools — a browser is just
another agent.

## What talks to what (ports)

| Port | What |
|---|---|
| host 30080 | MCP + UI (loopback-bound by kind config) |
| host 30001–30020 | per-endpoint Postgres NodePorts (M1 cap: 20 endpoints) |
| host 30099 / 30098 | storage-controller / pageserver APIs (debug) |
| pod 55433 | Postgres in every compute |
| pod 3080 | compute_ctl API (JWT; /terminate, /status) |
