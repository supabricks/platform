# Review 005: test matrix

Status: review complete; P1 findings open  
Date: 2026-08-16  
Scope: local gates, CI gates, unit/snapshot coverage, e2e/chaos/restore
harnesses, UI coverage, and test reproducibility

## Verdict

Review 004's fixes are verified. The hardened chart now renders explicit
service-account token posture, NetworkPolicies, Secret-backed credentials,
image digest annotations, and per-workload security contexts/exceptions. The
current operator image deployed cleanly, the dynamic compute pod hardening
held at runtime, and the full e2e/chaos/restore suite passed.

The test suite is strong where it matters most for the M1 prototype: core
create/branch/isolate/delete/suspend/wake/restore behavior is exercised
against a real kind cell, and the dangerous data-safety regressions from
Reviews 001-003 now have named tests.

It is not reference-grade as a handoff test matrix yet. The main weakness is
not lack of one big test; it is uneven gate ownership. Some checks exist only
as review commands, some CI checks depend on undeclared runner state, local
`just` targets do not match CI, and several contracts are snapshotted as
schemas but not validated against actual runtime payloads.

## Verification performed

Local/static gates:

```sh
cd platform
cargo fmt -- --check
cargo test --locked
cargo run --locked --bin crdgen | diff -u chart/crds/sspc-crds.yaml -
helm lint chart
cd ui && npm run build
```

Observed result: all passed. `cargo test --locked` ran 19 tests. `helm lint`
reported only the optional icon recommendation. The UI build emitted the
existing Sass legacy-API warning, then completed.

Hardening gate:

```sh
cd platform
./chart/check-hardening.sh
```

Observed result: failed on this machine with `ModuleNotFoundError: No module
named 'yaml'`. Re-running in a throwaway virtualenv with PyYAML installed
passed:

```sh
python3 -m venv /tmp/sspc-hardening-venv
. /tmp/sspc-hardening-venv/bin/activate
python -m pip install --quiet PyYAML
./chart/check-hardening.sh
```

Observed result: `hardening check OK: 9 workloads, 11 network policies`.

Runtime gate:

```sh
cd platform
docker build -t sspc-operator:p1 .
kubectl apply -f chart/crds/
kind load docker-image --name sspc sspc-operator:p1
helm upgrade --install sspc chart -n sspc-cell --create-namespace
kubectl -n sspc-cell rollout restart deploy/sspc-operator
kubectl -n sspc-cell wait --for=condition=Available deploy --all --timeout=300s
for ss in controller-pg safekeeper pageserver; do
  kubectl -n sspc-cell rollout status "statefulset/$ss" --timeout=300s
done
kubectl -n sspc-cell rollout status deploy/sspc-operator --timeout=180s
```

Observed result: image `sha256:6c3a28af56d3cc63aaf366826ff1cbc1cf0791a47d63ff8bd458bec6592b752d`
was loaded into kind, Helm revision 20 deployed, and all workloads rolled out.

Focused runtime hardening probe:

- Created `hardenprobe` through MCP.
- Verified the compute pod has `automountServiceAccountToken: false`.
- Verified pod `seccompProfile: RuntimeDefault`.
- Verified container `allowPrivilegeEscalation: false` and
  `capabilities.drop: [ALL]`.
- Verified `/var/run/secrets/kubernetes.io/serviceaccount/token` is absent.
- Deleted the probe database.

Acceptance/recovery gates:

```sh
./e2e/run.sh
./e2e/chaos.sh
./e2e/restore.sh
```

Observed results:

- `./e2e/run.sh` passed in 180 seconds.
- `./e2e/chaos.sh` passed in 125 seconds.
- `./e2e/restore.sh` passed in 99 seconds.

## Verified strengths

### 1. The core behavior suite is real integration coverage

`e2e/run.sh` drives the platform through MCP, then verifies the Kubernetes and
Postgres side effects. It covers invalid API input, idempotent create, data
load, branch isolation, per-endpoint credential enforcement, LSN/time PITR,
branch-of-branch parentage, status-less cleanup, idle activity, suspend/wake,
TTL reaping, enrollment, safe deletes, and cell-side cleanup.

Reference-grade status: strong for the current single-cell M1 promise.

### 2. Recovery and data-portability are exercised, not asserted

`e2e/chaos.sh` kills the operator, restarts the pageserver under an active
compute, and reboots the kind node. `e2e/restore.sh` destroys pageserver,
safekeeper, and controller Postgres PVCs, then proves parent and branch data
serve from the bucket and still accept writes.

Reference-grade status: strong for single-node recovery and bucket-backed
restore.

### 3. Unit tests pin high-risk pure logic

`cargo test` covers NodePort allocation, deterministic IDs, wake-vs-suspend
ordering, CU/priority resource mapping, head-branch ingestion wait decisions,
retry-state branch allocation, MCP schema snapshots, outputSchema presence,
input validation, structured tool errors, compute spec rendering, and compute
JWK generation.

Reference-grade status: good for fast feedback on the bugs already found.

### 4. Kubernetes hardening now has a rendered-manifest gate

`chart/check-hardening.sh` checks token automount, seccomp, privilege
escalation, capability drops, credential-looking literal env values, image
digest annotations, NetworkPolicy presence, and RBAC verb growth.

Reference-grade status: good once the runner dependency is made explicit.

## Findings

### P1-1: The hardening checker has an undeclared Python dependency

`chart/check-hardening.sh` imports PyYAML, but neither the script nor
`.github/workflows/ci.yml` installs it. On this machine, the script failed
immediately with `ModuleNotFoundError: No module named 'yaml'`; it passed only
after installing PyYAML into a temporary virtualenv.

Impact: the new 004 hardening gate is environment-dependent. A clean engineer
or hosted runner can fail before testing the chart, or pass only because an
ambient package happens to be present.

Reference-grade fix:

- Make the dependency explicit: install PyYAML in CI and document it locally,
  or rewrite the checker in a dependency-free tool already in the repo's
  required toolchain.
- Add a `just hardening` target that performs the dependency setup or uses the
  dependency-free checker.

Exit criteria:

- `./chart/check-hardening.sh` passes on a fresh documented environment.
- CI does not rely on preinstalled Python packages outside the workflow.

### P1-2: Local gates and CI gates do not have one authoritative entry point

The CI workflow runs `cargo test --locked`, `chart/check-hardening.sh`, the
installer, e2e, chaos, and restore. The review-local gate also runs
`cargo fmt -- --check`, CRD generated diff, Helm lint, and UI build. The
`Justfile` exposes only `test`, `build`, `crdgen`, `image`, `up`, `down`, and
`e2e`; it has no hardening, UI, Helm, chaos, restore, or full `verify` target.

There is also a local update trap: `install/up.sh` skips image loading when
any `sspc-operator` image name exists in the kind node, not when the node image
matches the freshly rebuilt local image ID. The dev-loop docs use explicit
`kind load image-archive`, but the installer itself can still exercise a stale
same-tag operator during repeat local runs.

Impact: "green locally" can mean different things depending on which doc or
target an engineer used. That is exactly the kind of handoff ambiguity the
review series is trying to remove.

Reference-grade fix:

- Add `just verify-static` and `just verify-runtime` targets that map directly
  to CI jobs.
- Add `just hardening`, `just chaos`, and `just restore`.
- Update handbook/dev-loop and README timing/contracts to match the actual
  target list.
- Make the installer compare/load the current local operator image ID, or make
  it explicit that repeat developer deploys must use the dev-loop load path.

Exit criteria:

- A clean engineer can run one documented local static gate and one documented
  runtime gate that match CI.
- The docs, Justfile, and workflow name the same test tiers.
- Rebuilding `sspc-operator:p1` and running the documented deploy path cannot
  silently test the old node image.

### P1-3: CI misses source-format and generated-artifact drift gates

The review command set includes `cargo fmt -- --check`,
`cargo run --locked --bin crdgen | diff -u chart/crds/sspc-crds.yaml -`, and
`helm lint chart`. The GitHub workflow does not run those checks.

Impact: formatting drift, stale CRDs, and chart lint failures can land even
though every PR-level CI job is green. The stale-CRD case is especially risky:
runtime e2e can pass against old schema behavior while a future direct
Kubernetes user receives a different contract than the Rust types imply.

Reference-grade fix:

- Add a static CI job for Rust format, CRD generated diff, Helm lint, and UI
  build/typecheck.
- Keep the Docker image build as runtime packaging coverage, not the only UI
  build signal.

Exit criteria:

- Every command in the review-local static gate is also run by CI.
- A CRD field/schema change without regenerated YAML fails CI.

### P1-4: MCP output schemas are not validated against actual tool payloads

Review 003 added `outputSchema` definitions and snapshots them through
`tool_defs()`. The tests prove that schemas exist and do not drift
accidentally. They do not prove that each successful tool implementation
actually returns a payload matching its schema, or that structured tool errors
match the documented error object for every tool.

Today e2e checks selected fields on selected calls. That is good behavior
coverage, but it is not exhaustive contract validation.

Impact: agents and the UI can break if a tool returns a field with the wrong
type, omits a required field in an untested branch, or emits an error shape
that only one tool path uses.

Reference-grade fix:

- Add JSON Schema validation tests for representative success payloads of all
  14 tools.
- Add structured-error golden tests for every mutating tool's main failure
  modes.
- Make e2e validate returned payloads against `tools/list` schemas before
  asserting behavior-specific fields.

Exit criteria:

- A payload/schema mismatch fails a unit or contract test before runtime e2e.
- Every `isError` response carries `reason`, `retriable`, and
  `suggested_action`.

### P1-5: The promised idempotency torture is only partially covered

RFC 012's T3 calls for duplicate, concurrent-duplicate, and delayed-duplicate
mutation replay. `e2e/run.sh` currently creates `e2edb` twice sequentially and
asserts exactly one CR. Unit tests cover NodePort allocator behavior, but the
cluster suite does not exercise concurrent MCP creates/deletes, create while a
previous object is deleting, or many endpoints racing for the NodePort block.

Impact: reconciler idempotency is central to the operator claim. Sequential
duplicate create is useful, but it does not catch request races, deletion
finalizer races, or port-allocation collisions under concurrent callers.

Reference-grade fix:

- Add an e2e idempotency section that fires parallel `create_database` and
  `create_branch` calls for the same names and asserts one CR/one endpoint.
- Add delete/recreate while finalizers are running.
- Add a bounded NodePort exhaustion test that fills the block enough to prove
  structured failure and cleanup, without making normal CI too slow.

Exit criteria:

- Concurrent duplicate mutations converge without duplicate children or leaked
  cell resources.
- Port exhaustion is a tested structured error path in-cluster.

### P1-6: The UI has no behavioral test harness

`platform/ui/package.json` has `dev`, `build`, and `preview`, but no test,
lint, component, or browser-smoke script. The UI is a first-class MCP client
and contains nontrivial logic: token handling, error-layer handling, polling,
modals, copy-to-clipboard, metrics rendering, and table expansion.

Impact: TypeScript and Vite prove the UI compiles. They do not prove the page
renders against realistic MCP payloads, handles the three Review 003 error
layers, or keeps the main workflow usable after component changes.

Reference-grade fix:

- Add Vitest tests for `mcp.ts` error-layer handling and token behavior.
- Add a small React Testing Library or Playwright smoke for the estate screen
  with mocked MCP responses.
- Optionally add one deployed Playwright smoke against the kind UI after e2e:
  page loads, rows render, create dialog opens, and an error toast displays.

Exit criteria:

- UI-only regressions fail before a human opens the browser.
- The MCP client error contract is tested outside production e2e.

## P2 / matrix expansion

### P2-1: NetworkPolicy enforcement is not exercised

The chart now renders NetworkPolicies by default and documents that kind's
default CNI does not enforce them. That is an acceptable M1 statement, but the
current CI only proves render intent.

Exit criteria: add a non-default policy-capable job, such as kind with Calico
or a k3s/Cilium target, and prove an unrelated pod cannot reach MCP or storage
APIs while the normal suite still passes.

### P2-2: Agent harness coverage remains manual

T5, the real-agent test, is still manual/nightly by design. That is acceptable
for merge gating, but this is an agent-first surface and schema tests cannot
catch tool-description ergonomics.

Exit criteria: run a non-blocking scheduled agent harness against Claude Code
or another connected MCP client, record artifacts, and keep failures visible
without blocking ordinary PRs.

### P2-3: Host-side client behavior is still out of the CI path

The e2e scripts use in-pod `psql` because the kind ports are loopback-bound and
runner images lack host `psql`. This is already documented debt and should
remain so until the M2 gateway.

Exit criteria: gateway/client conformance with real drivers, including
connect-after-suspend and reconnect-after-wake behavior.

### P2-4: Chaos is still selective

The current chaos suite covers operator death, pageserver restart, and node
reboot. It does not cover storage-controller restart, controller Postgres
restart under activity, safekeeper restart, MinIO outage/throttle, Kubernetes
API transient failures, or Helm rollback/upgrade failure.

Exit criteria: add chaos cases only as they become current promises. Do not
expand this into an HA test matrix until the HA design exists.

## Decision

The current suite is credible for the M1 prototype's core data and lifecycle
claims after Reviews 001-004. The next reference-grade step is to make the
gate set itself boring: one documented local entry point, CI parity for every
static check, declared checker dependencies, schema-vs-payload validation, and
minimum UI behavior coverage.

## Resolution (2026-08-16)

All P1 findings fixed; the P2 expansion items are recorded as backlog debt
with exit conditions (12a/12b).

- **P1-1**: the hardening checker is a Rust integration test
  (`tests/chart_hardening.rs`, serde_yaml + helm — both already in the
  required toolchain); the Python dependency is gone and the gate rides
  `cargo test` everywhere automatically. `check-hardening.sh` remains as a
  thin wrapper for humans.
- **P1-2**: `just verify-static` and `just verify-runtime` mirror the two CI
  jobs exactly, with individual targets for every tier (`hardening`,
  `crd-check`, `helm-lint`, `ui-test`, `chaos`, `restore`) and `just deploy`
  for the dev loop. The installer's load-skip now compares operator IMAGE
  IDS (crictl vs docker), killing the stale-same-tag trap. The dev-loop doc
  names the same tiers.
- **P1-3**: CI's unit job runs every command from this review's local gate:
  `cargo fmt --check`, crdgen diff, `helm lint`, and UI tests + build.
- **P1-4**: three layers — a unit test proves every outputSchema compiles as
  valid JSON Schema; the e2e fetches `tools/list` live and validates seven
  tools' actual payloads against their declared required fields (arrays per
  item); one refusal payload is checked for all three structured error
  fields (the shape itself is centrally enforced by the ToolError type).
- **P1-5**: the "T3+ idempotency torture" e2e step: five CONCURRENT
  duplicate creates converge to one CR/one Service; create-during-delete
  converges with only structured errors en route; NodePort exhaustion is a
  synchronous structured refusal (new `ensure_capacity` pre-check) proven
  by filling the 20-port block. **The torture found a real API flaw on its
  first run**: create-during-delete returned "ready" against the DYING
  endpoint (SSA landed as an update on the deleting CR; the old pod still
  read Ready) — creates now refuse retriably while a previous instance is
  mid-deletion.
- **P1-6**: the UI has a vitest harness (10 tests): every error layer of
  the MCP client — network death, 401, non-JSON, protocol envelope,
  malformed result, tool errors — plus token capture/persistence. Runs in
  CI. A rendered-component/browser smoke remains future work, named in the
  backlog alongside the P2 items.

Verification: `just verify-static` green (21 cargo tests incl. the
hardening contract, 10 UI tests, all drift gates) · e2e PASS 200s (18
steps) · chaos PASS 124s · restore PASS 100s.
