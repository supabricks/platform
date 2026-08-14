# Review 003: API contract

Status: review complete; P1 findings open  
Date: 2026-08-13  
Scope: MCP streamable HTTP surface, tool schemas, tool results, errors, UI client assumptions, and docs alignment

## Verdict

The MCP surface is usable and the happy-path contract is much stronger after
Review 001: the source exposes 14 tools, the tool input schema is snapshot
tested, tool count is pinned by a unit test, errors from `tools/call` are
structured, and the UI uses the same MCP facade as agents.

It is not reference-grade yet. The current contract still validates too little
at the API boundary, silently coerces some caller input, and only snapshots
input schemas. Result payloads, protocol-level errors, and edge-case tool
semantics can drift or surprise clients without a test failing.

## Verification performed

Local gates:

```sh
cd platform
cargo fmt -- --check
cargo test --locked
cargo run --locked --bin crdgen | diff -u chart/crds/sspc-crds.yaml -
helm lint chart
cd ui && npm run build
```

`cargo fmt -- --check` initially failed on `reconcile.rs`; `cargo fmt` was run
and the Rust gates then passed with 17 tests. `helm lint` and the UI build also
passed.

Runtime gate:

```sh
cd platform
docker build -t sspc-operator:p1 .
kind load docker-image --name sspc sspc-operator:p1
helm upgrade --install sspc chart -n sspc-cell
kubectl -n sspc-cell rollout restart deploy/sspc-operator
kubectl -n sspc-cell rollout status deploy/sspc-operator --timeout=180s
./e2e/run.sh
```

Observed result: `./e2e/run.sh` passed in 185 seconds against the current
operator image.

Focused probes:

- Malformed JSON returns HTTP 400 with `{"error":"bad json"}`.
- Unknown JSON-RPC method returns a JSON-RPC error envelope with HTTP 200.
- `create_database` with `priority:"urgent"` succeeds and persists
  `spec.priority=Standard`.
- `create_database` with `cu_limit:-5`, `suspend_after_seconds:-1`, and
  `ttl_seconds:-10` succeeds; the negative TTL then makes the database reap
  immediately.
- `create_branch` with `database:"doesnotexist"` returns
  `{"status":"provisioning"}` and creates a Branch CR with no phase/message.

## Verified strengths

### 1. The tool list is pinned

`tool_defs()` is snapshot-tested in `mcp-tools.json`, and a separate unit test
pins the count at 14. This directly addresses Review 001's doc/source drift.

Reference-grade status: good for input schema drift.

### 2. Tool-layer errors are structured

Tool failures returned through `tools/call` serialize as JSON with `reason`,
`retriable`, and `suggested_action`. That is the right shape for agents.

Reference-grade status: good for failures that reach the tool layer.

### 3. The UI uses the same contract as agents

The browser calls `/mcp` with `tools/call` instead of using a private REST API.
This keeps the UI honest and makes e2e coverage valuable for both humans and
agents.

Reference-grade status: good.

### 4. The main workflows are exercised end-to-end

The e2e suite drives create, idempotent create, load, immediate branch,
point-in-time branch, branch-of-branch, credentials, status-less cleanup,
session churn, suspend/wake, TTL, enrollment, safe deletes, and storage cleanup
through MCP.

Reference-grade status: good for happy paths and the major Review 001/002
regressions.

## Findings

### P1-1: `create_branch` does not validate the owning database up front

`create_branch` validates an optional parent branch, but it does not validate
that `database` is a valid resource name or that the database exists before
creating the Branch CR. A request such as:

```json
{"name":"apimissing","database":"doesnotexist"}
```

returns `{"status":"provisioning"}` after the 30-second wait and leaves a CR
with no useful status message. The reconciler will keep retrying a parent
lookup that can never succeed.

Impact: agents cannot distinguish "slow provisioning" from a caller error, and
the estate can accumulate stuck branch CRs from typos.

Reference-grade fix:

- Validate `database` with the same naming rules as `name`.
- Check that the owning Database exists before applying the Branch.
- Return a non-retriable structured tool error if it is missing.

Exit criteria:

- Bad database names and missing databases fail synchronously through
  `tools/call`.
- Add unit or e2e coverage for missing database and mixed-case database input.

### P1-2: Numeric fields accept nonsensical values

The MCP tools and CRDs accept negative or zero values for fields where the
contract implies positive bounds:

- `cu_limit:-5` persists as `spec.cuLimit=-5`; pod resources clamp internally,
  but `get_cu_ledger` still uses the raw spec value.
- `suspend_after_seconds:-1` disables suspend because the lifecycle loop
  returns early for `<= 0`.
- `ttl_seconds:-10` reaps immediately because every created object is already
  older than a negative TTL.

Impact: agents can receive successful responses for inputs that mean something
very different from what the schema descriptions imply.

Reference-grade fix:

- Put min/max bounds in CRD schema for `cuLimit`, `suspendAfterSeconds`, and
  `ttlSeconds`.
- Mirror those checks in MCP so callers get structured tool errors before CRs
  are created.
- Decide and document whether `suspend_after_seconds=0` means "never suspend";
  if so, expose that explicitly in schema/docs.

Exit criteria:

- Negative numeric values are rejected by both MCP and direct Kubernetes apply.
- Ledger/resource accounting cannot diverge because of clamped runtime values.

### P1-3: Invalid enum input is silently coerced

`priority` has an enum in the tool schema and CRD, but the MCP implementation
maps any string other than `"high"` or `"low"` to `Standard`. The probe
`priority:"urgent"` succeeded and persisted `Standard`.

Impact: callers can believe a priority was honored when the platform silently
ignored it.

Reference-grade fix:

- Reject unknown priority values in MCP with a non-retriable structured error.
- Add a unit test for invalid priority on database and branch creation.

Exit criteria:

- The only accepted MCP priority values are exactly the schema enum values.

### P1-4: Result schemas are not part of the pinned contract

The snapshot covers `tools/list` input schemas only. Result payloads are shaped
by ad hoc JSON in each tool and by e2e assertions for selected fields. There is
no generated or snapshot-tested contract for fields such as `connection_uri`,
`woke_from_suspend`, `wake_seconds`, list row shapes, metric rows, event rows,
or ledger numbers.

Impact: result-field drift can break agents or the UI without failing the tool
schema snapshot.

Reference-grade fix:

- Add result schema definitions for every tool, either in the MCP fixture or a
  parallel contract fixture.
- Add golden tests for success and structured-error payloads for each tool.
- Keep e2e as behavior coverage, not the only contract guard.

Exit criteria:

- A result field rename/removal fails a contract test before runtime e2e.

### P1-5: Protocol-level errors are not consistently JSON-RPC-shaped

Tool errors are structured, but failures before dispatch are inconsistent:

- Malformed JSON returns HTTP 400 with `{"error":"bad json"}` rather than a
  JSON-RPC parse-error envelope.
- Unauthorized requests return HTTP 401 with `{"error":"invalid authorization token"}`.
- Unknown methods return a JSON-RPC error envelope with HTTP 200.

Some of this is acceptable HTTP behavior, but it is not a single documented
client contract.

Impact: generic MCP clients and the UI need special cases for transport errors,
JSON-RPC errors, and tool errors. Today the UI assumes a successful
`body.result.content[0].text` shape and only special-cases 401.

Reference-grade fix:

- Document the three error layers: HTTP auth/parse, JSON-RPC protocol, and
  tool-level `isError`.
- Prefer JSON-RPC-compliant envelopes for parse/protocol errors where possible.
- Harden the UI client to handle missing `result`, protocol `error`, bad JSON,
  and network failures without throwing unrelated parsing exceptions.

Exit criteria:

- Protocol error shapes are tested and documented.
- The UI can display useful messages for all three error layers.

## P2 / documented prototype limits

### P2-1: GET/SSE is compatibility glue, not full stream semantics

The GET leg holds an idle SSE stream with keep-alives. That is enough for the
known clients that require a streamable-HTTP server stream, but there is no
session model, resumability, or server-side notifications.

Exit criteria: either document this as "keep-alive only" or implement the
fuller stream semantics required by future clients.

### P2-2: Identifier normalization is surprising

Top-level `name` values are lowercased before validation. The UI also slugs
names. That is convenient, but direct agents can send `Prod` and operate on
`prod`.

Exit criteria: choose one contract: reject non-lowercase names, or document
normalization and return the canonical name prominently in every response.

### P2-3: Source comments still describe the pre-SSE posture

The module comment in `mcp.rs` still says "no SSE" while the implementation now
serves the GET/SSE keep-alive leg.

Exit criteria: update the source comment so local readers do not rediscover
Review 001's old MCP drift.

## Decision

The current API is good enough for M1.5 demos and the existing e2e suite, but
not yet reference-grade. The next implementation pass should harden the MCP
boundary: validate inputs synchronously, reject invalid enum/numeric values,
add result-contract fixtures, and make protocol errors explicit. Those changes
would turn the MCP facade from "works with known clients" into a stable agent
contract.

## Resolution (2026-08-14)

All P1 and P2 findings fixed; each probe from this review is now an e2e step.

- **P1-1**: `create_branch` validates the owning database's name (same rules,
  normalized) and existence before any CR is applied — `doesnotexist` is a
  synchronous non-retriable error and no Branch CR is created (e2e-asserted).
- **P1-2**: numeric bounds enforced twice — CRD structural schema
  (`cuLimit` 1–960, `suspendAfterSeconds` 0–86400, `ttlSeconds` ≥1) guards
  direct kubectl apply (e2e-asserted); MCP `bounded_int` mirrors them with
  structured errors. `suspend_after_seconds: 0` = never suspend, documented
  in schema and handbook.
- **P1-3**: priority is strict — `"urgent"` errors; only the enum values
  parse (unit + e2e).
- **P1-4**: every tool declares an **outputSchema**; result contracts live in
  the same snapshot fixture as input schemas (drift fails CI) and are served
  via `tools/list`. A unit test forbids schema-less tools.
- **P1-5**: three error layers documented in the module header; malformed
  JSON now returns a JSON-RPC parse-error envelope (-32700, e2e-asserted);
  the UI client handles network failure, non-JSON, protocol errors, and
  malformed tool responses with readable messages.
- **P2-1/2/3**: GET/SSE documented keep-alive-only; name normalization
  documented with canonical-name echo (unit-tested `Prod`→`prod`); the stale
  "no SSE" comment replaced by the error-contract doc.

**Found live while verifying (the big one)**: the storage-controller HTTP
client had NO timeout. One hung request — hit for real when a full node disk
wedged controller-pg — parked that CR's reconcile future FOREVER: kube-rs
dedups events for in-flight objects, so the CR never reconciled again until
operator restart, with no log and no status. All storcon calls now carry
5s-connect/30s-request timeouts, converting the silent permanent wedge into
loud bounded retries. The disk-full cascade (kind-load layer accumulation →
controller-pg PANIC → storcon hang) and the prune-eats-the-compute-image
trap are both in the runbook; the installer's image-load skip now checks the
compute image too.

Verification: cargo test 19/19 · e2e PASS 179s (17 steps) · chaos PASS 123s ·
restore PASS 97s.
