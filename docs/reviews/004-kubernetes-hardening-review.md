# Review 004: Kubernetes hardening

Status: review complete; P1 findings open  
Date: 2026-08-15  
Scope: Helm chart rendering, pod security posture, RBAC, service-account
tokens, network boundaries, image provenance, secrets, probes, and resource
policy

## Verdict

Review 003's fixes are verified. The current operator image passed the local
gates, deployed cleanly into kind, passed the e2e suite, and returned the
expected structured failures for the API-contract edge cases.

The Kubernetes baseline is better than it was in Review 001: the operator and
notify-sink now run with explicit non-root/seccomp/no-capability/read-only-root
settings, the operator has readiness/liveness probes, the installer binds host
ports to loopback, and the install path pulls upstream images by digest before
retagging them for kind.

It is still not reference-grade as a Kubernetes handoff. The chart does not yet
make the namespace boundary explicit: most pods receive Kubernetes API tokens
they do not need, there are no NetworkPolicies, storage credentials are
rendered as literal environment values, and the stock-image hardening exception
is too broad to be a maintainable contract.

This review does not expand M1 into TLS, IAM/OIDC, gateway, or HA. Those remain
deferred. The bar here is that the current single-node prototype should have
clear, testable Kubernetes defaults that a new engineer cannot accidentally
weaken.

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

Observed result: all passed. `cargo test --locked` ran 19 tests. `helm lint`
reported only the optional icon recommendation. The UI build emitted the
existing Sass legacy-API warning, then completed.

Runtime gate:

```sh
cd platform
docker build -t sspc-operator:p1 .
kubectl apply -f chart/crds/
kind load docker-image --name sspc sspc-operator:p1
helm upgrade --install sspc chart -n sspc-cell --create-namespace
kubectl -n sspc-cell rollout restart deploy/sspc-operator
kubectl -n sspc-cell rollout status deploy/sspc-operator --timeout=180s
./e2e/run.sh
```

Observed result: image `sha256:d056ce8bbb4a9721eb7c7bf8c124fa1c6b2c57ac96637c749fae524ba34324a6`
was loaded into the kind node, Helm revision 16 deployed, the operator rolled
out successfully, and `./e2e/run.sh` passed in 189 seconds.

Focused Review 003 probes passed against the deployed `/mcp` endpoint:

- Malformed JSON returns HTTP 400 with JSON-RPC error code `-32700`.
- Unknown methods return JSON-RPC method-not-found code `-32601`.
- Invalid priority is rejected and creates no Database CR.
- Invalid numeric fields are rejected and create no Database CR.
- The e2e Review 003 block also proves direct `kubectl apply` rejects a
  negative `cuLimit` through the CRD schema.
- Missing branch database is rejected and creates no Branch CR.
- `tools/list` returns 14 tools, all with `outputSchema`.

Static chart checks:

```sh
helm template sspc platform/chart -n sspc-cell > /tmp/sspc-rendered.yaml
rg -n "automountServiceAccountToken|NetworkPolicy|PodDisruptionBudget|securityContext|image:|imagePullPolicy|nodes/proxy" /tmp/sspc-rendered.yaml
```

Observed result: no `automountServiceAccountToken`, `NetworkPolicy`, or
`PodDisruptionBudget` manifests are rendered. Operator and notify-sink render
explicit security contexts; broker, storage-controller, controller-pg, MinIO,
pageserver, safekeeper, and the MinIO bucket job do not.

## Verified strengths

### 1. The custom workloads have a real baseline

The operator runs as service account `sspc-operator`, with pod-level
`runAsNonRoot` and `seccompProfile: RuntimeDefault`, and container-level
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, and
`readOnlyRootFilesystem: true`. It also has readiness and liveness probes on
the MCP/UI listener.

The notify-sink runs as UID 65534, with the same seccomp/no-escalation/no-caps/
read-only-root posture.

Reference-grade status: good for workloads this repo owns.

### 2. Host exposure is deliberately loopback-bound in the installer

The kind config maps MCP, debug ports, and endpoint NodePorts to
`listenAddress: "127.0.0.1"`. That matches the documented open-mode MCP stance:
acceptable for a laptop prototype, not an internet-facing control plane.

Reference-grade status: acceptable for M1, as long as docs keep saying this is
loopback/kind scope.

### 3. The installer pins upstream images by digest

`platform/install/up.sh` pulls Neon, compute-node, postgres, MinIO, mc, and
busybox by digest, then retags them to the chart's local names before loading
kind. That gives the one-command path reproducible upstream inputs.

Reference-grade status: good for the installer path, but incomplete in the
Helm-rendered contract. See P1-4.

### 4. Workloads are not BestEffort

Every rendered workload has CPU and memory requests. The values file documents
requests-only as intentional for the shared storage cell.

Reference-grade status: acceptable for the current kind cell, with future
quota/limit policy deferred until real cluster targets exist.

## Findings

### P1-1: Non-operator pods should not receive Kubernetes API tokens

The chart renders no `automountServiceAccountToken: false` anywhere. The
operator needs API credentials; the broker, pageserver, safekeeper,
storage-controller, controller-pg, MinIO, bucket job, and notify-sink do not.
Today those pods run under the namespace default service account unless the
cluster default says otherwise.

Impact: compromise of any storage or utility container can become Kubernetes
API access, even though that access is not part of the workload contract.

Reference-grade fix:

- Set `automountServiceAccountToken: false` on every pod template and job
  template that does not call the Kubernetes API.
- Set the operator's token behavior explicitly so the exception is visible.
- Consider a chart-level default helper so new templates opt out by default.

Exit criteria:

- Rendered manifests show exactly one API-token consumer: the operator.
- A non-operator pod does not have the service-account token mounted at
  `/var/run/secrets/kubernetes.io/serviceaccount/token`.
- e2e still passes with token automount disabled for non-operator pods.

### P1-2: There is no in-cluster network boundary

The rendered chart has Services for MCP, storage-controller, pageserver,
safekeeper, broker, MinIO, controller-pg, and endpoint computes, but no
NetworkPolicies. Loopback-bound kind host ports protect access from outside the
host. They do not restrict lateral traffic from any pod that can run in the
namespace or cluster.

Impact: a compromised pod can call the MCP endpoint, storage-controller API,
pageserver API, controller Postgres, MinIO, or compute endpoints directly. That
turns every workload compromise into a broad cell compromise.

Reference-grade fix:

- Add a default-deny policy for the `sspc-cell` namespace.
- Add allow policies for the required edges only: operator to Kubernetes API,
  operator to storage-controller and computes, computes to safekeeper/
  pageserver, pageserver/safekeeper to object storage, storage-controller to
  controller-pg and notify-sink, and host-facing NodePort paths required by M1.
- Make network-policy enforcement explicit in the supported-cluster story.
  Kind's default CNI may not enforce policies, so either document that local
  kind is a render/conformance check only or use a CNI that enforces them.

Exit criteria:

- `helm template` renders NetworkPolicies by default or behind an explicitly
  enabled hardening value.
- A negative test proves an unrelated pod cannot reach MCP or storage APIs.
- The normal e2e suite still passes with policy enforcement enabled.

### P1-3: Storage credentials are rendered as literal environment values

The chart passes S3 credentials directly as environment `value` fields into
pageserver and safekeeper, uses literal MinIO root credentials, and sets the
controller Postgres password directly in the StatefulSet. These values also
live in Helm values and rendered manifests.

Impact: anyone who can read rendered manifests, Helm release state, or pod
specs can read storage credentials. The current defaults are demo credentials,
but the same chart path is how a user points at a real object store when
`demoMinio.enabled=false`.

Reference-grade fix:

- Move S3 and controller Postgres credentials into Kubernetes Secrets.
- Support `existingSecret` values for externally managed credentials.
- Use `valueFrom.secretKeyRef` in every workload.
- Keep demo defaults only as generated or clearly demo-scoped Secrets, not
  literal PodSpec values.

Exit criteria:

- `helm template` does not render real credential strings in pod environment
  values.
- The installer path still works from a clean clone.
- Docs explain how to supply customer object-store credentials without putting
  them in versioned values files.

### P1-4: Image provenance is split between installer and chart

The installer pins upstream images by digest, but the chart values render local
or mutable names: `ghcr.io/neondatabase/neon:latest`,
`ghcr.io/neondatabase/compute-node-v16:latest`, `minio/mc:latest`,
`postgres:16-alpine`, `busybox:1.36`, and `sspc-operator:p1`, all with
`imagePullPolicy: Never`.

That is practical for kind, but the rendered Kubernetes contract does not say
which digest is running. A direct Helm user or a future cluster path can drift
from the installer pins without a chart diff making the change obvious.

Impact: a supposedly reference-grade install cannot be audited from Helm output
alone. Debugging "same tag, different image" failures will waste time and can
undermine security review.

Reference-grade fix:

- Represent images in values as digest-aware structured data or full pinned
  references.
- Keep a local-kind override for `imagePullPolicy: Never`, but make the pinned
  digest the source of truth.
- Add a CI check that compares `install/up.sh` pins and chart values, or remove
  the duplicated source of truth entirely.

Exit criteria:

- The rendered chart or workload annotations expose the exact expected digest
  for every non-local image.
- Changing an upstream image digest is a visible review diff.
- The installer and Helm values cannot silently disagree.

### P1-5: The stock-image security exception is too broad

`values.yaml` documents that stock Neon, postgres, and MinIO images "run as
shipped." That was a reasonable Review 001 exit for not breaking the prototype,
but it is not yet a reference-grade contract. The rendered stock workloads lack
explicit pod/container security contexts, including seccomp, privilege
escalation, dropped capabilities, and user/group settings.

Impact: future changes cannot tell whether an omitted setting is required by
the image, forgotten, or never tested. A broad exception also makes it easy to
add new stock containers with no hardening review.

Reference-grade fix:

- Build a workload-by-workload hardening matrix for broker, storage-controller,
  controller-pg, MinIO, mc, pageserver, safekeeper, and computes.
- Apply safe controls where the image supports them. At minimum, test
  `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`, and
  `capabilities.drop: [ALL]`.
- For controls that cannot be applied, document the exact reason and the test
  that proves the constraint.

Exit criteria:

- Every rendered workload has either an explicit hardened security context or
  a named, tested exception.
- The chart has a regression test or rendered-manifest check for that matrix.
- e2e passes with the tightened stock workload posture.

## P2 / documented prototype limits

### P2-1: Operator RBAC is namespace-wide and node metrics are cluster-scoped

The operator Role can get/list/watch/create/patch/delete pods, services,
configmaps, and secrets across the whole namespace. It also has a ClusterRole
for `nodes` and `nodes/proxy` to read kubelet Summary API metrics.

This is acceptable only if `sspc-cell` is treated as an operator-owned cell
namespace. It is not a multi-tenant namespace boundary.

Exit criteria: document the namespace ownership model, make kubelet
`nodes/proxy` metrics optional or replace them with a narrower metrics source,
and add tests that fail if RBAC expands without review.

### P2-2: No PDBs is correct for M1, but must stay explicit

The design already says single replicas everywhere and no
PodDisruptionBudgets. That is fine for kind and for the current non-HA claim.

Exit criteria: do not add availability claims until operator leader election,
multi-safekeeper identity, pageserver failover, and PDB/topology policy are
designed and tested together.

### P2-3: Probe policy is implicit

The operator has readiness/liveness probes. Storage components generally have
readiness probes only, and notify-sink has no probe. That may be correct:
liveness probes on stateful storage can cause harmful restarts if they are not
designed carefully.

Exit criteria: write the probe policy down per workload: what readiness means,
why liveness exists or is intentionally absent, and which failures are left to
the reconciler or Kubernetes restart policy.

### P2-4: Requests-only needs a cluster policy story later

Every pod has resource requests and no limits. That is intentionally documented
for the shared storage cell, and it is a reasonable laptop default.

Exit criteria: before any shared-cluster or production-like target, add the
Namespace `ResourceQuota`/`LimitRange` posture or explain why this chart must
run in a dedicated cluster/namespace with external policy.

## Decision

The Review 003 implementation fixes are verified.

The current chart is acceptable for the documented loopback-bound, single-node
M1 prototype. It should not be described as Kubernetes-hardened or
reference-grade until the P1 items above are fixed or converted into precise,
tested exceptions.

## Resolution (2026-08-15)

All P1 findings fixed; all P2 stances written down as contract. The whole
page is enforced by `platform/chart/check-hardening.sh` (a rendered-manifest
check, run in CI), and the exception matrix lives in
`platform/chart/hardening.md`.

- **P1-1**: `automountServiceAccountToken: false` on every pod and job
  template including operator-created compute pods; the operator carries an
  explicit `true` — the check fails any second token consumer.
- **P1-2**: default-deny ingress + 10 explicit edge policies render by
  default (`networkPolicies.enabled`). Host-facing ports admit all sources
  (NodePort traffic arrives from the node; the guard is loopback binding).
  kind's CNI non-enforcement is documented in hardening.md — on the demo
  cluster this is a rendered, CI-checked contract; enforcement needs a
  policy-capable CNI (real-cluster story).
- **P1-3**: S3 and controller-Postgres credentials live in Secrets
  (`sspc-s3`, `sspc-controller-pg`) with `existingSecret` overrides for real
  deployments; every consumer uses secretKeyRef/envFrom (the storage
  controller composes its database URL from `$(DATABASE_PASSWORD)`); the
  check fails any credential-looking literal env value.
- **P1-4**: `chart/values.yaml` image entries carry name + pinned digest;
  the installer PARSES its pull list from values.yaml (duplicate source of
  truth deleted); every pod exposes `sspc.io/image-digest`; the check fails
  a missing annotation. Exception: operator-created computes (tag-only env),
  named in hardening.md.
- **P1-5**: the blanket stock-image exception is replaced by a per-workload
  matrix: everything runs seccomp RuntimeDefault + no-privilege-escalation +
  drop-ALL; controller-pg runs directly as uid 70 (no su-exec, no
  SETUID/SETGID); minio stays root for the root-owned demo PVC (uid-0 with
  zero caps = plain file access). Two empirical finds along the way:
  Job templates are immutable (the seed job is now a helm hook, recreated
  per upgrade) and dropping CAP_DAC_OVERRIDE exposed the mc image's
  unwritable /root (fixed with HOME=/tmp, not a returned capability).
- **P2-1..4**: namespace-ownership model, probe policy per workload, the
  no-PDB/no-limits stances — all written in hardening.md; the operator
  Role's verb sets are pinned in the check so RBAC growth is a review event.

Verification: full hardened rollout on the live cell, then e2e PASS 231s
(17 steps), chaos PASS 126s, restore PASS 99s — the named exceptions are
each proven by the gate that exercises them (fresh-PVC init as uid 70,
bucket surviving restore under caps-drop, computes waking hardened).
