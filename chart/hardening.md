# Chart hardening contract

Scoped by the Kubernetes hardening review
(`docs/reviews/004-kubernetes-hardening-review.md`).
`./chart/check-hardening.sh` enforces everything on this page against the
rendered manifests — it runs in CI and fails on any weakening. A new workload
must satisfy the baseline or add a **named exception here** and in the
script's exception lists.

## Baseline (every workload)

- `automountServiceAccountToken: false` — the **operator is the only
  Kubernetes API consumer** (explicit `true`, P1-1).
- Pod `seccompProfile: RuntimeDefault`; every container
  `allowPrivilegeEscalation: false` + `capabilities.drop: [ALL]`.
- No credential as a literal env value — Secrets only (`sspc-s3`,
  `sspc-controller-pg`, or `*.existingSecret` overrides).
- Pod annotation `sspc.io/image-digest` carries the pinned upstream digest;
  `chart/values.yaml` digests are the single source of truth and the
  installer derives its pull list from them.
- Resource requests on every container; requests-only (no limits) is the
  documented cell posture (see values.yaml).

## Named exceptions

| Workload | Exception | Why | Proven by |
|---|---|---|---|
| operator | API token mounted | it IS the control plane | RBAC verb-set pinned in check script |
| minio (+ mc job) | runs as root (no `runAsNonRoot`) | existing demo-PVC data is root-owned; local-path volumes get no fsGroup relabeling. All caps dropped → uid-0 is plain file access | restore drill (bucket survives) |
| controller-pg | `runAsUser/fsGroup: 70` instead of image default root→su-exec | running as the postgres uid directly avoids needing SETUID/SETGID caps | e2e + restore (fresh PVC init as uid 70) |
| neon images (pageserver, safekeeper, broker, storage-controller) | no `runAsNonRoot` assertion | image user is upstream's contract; caps are dropped regardless | e2e + chaos + restore |
| compute pods (operator-created) | no digest annotation | the operator knows only the tag (`SSPC_COMPUTE_IMAGE`); the digest is pinned in values + installer | — |
| notify-sink | no probes | a 12-line busybox loop; the storage controller retries, and drill-1 covers operator-side effects | chaos drill 1 |

## Probe policy

- **operator**: readiness + liveness on the MCP listener — it serves traffic
  and a wedged runtime should restart (state is in CRs; restart is always
  safe, see runbook invariants).
- **pageserver / safekeeper / controller-pg / minio**: readiness only.
  Deliberate: liveness on stateful storage causes harmful restarts during
  slow-but-correct recovery (WAL replay, re-attach). Failure handling belongs
  to the reconciler and to Kubernetes restart policy on process exit.
- **storage-controller**: readiness only; its health is also observed by the
  pageserver's re-attach retries (loud in logs, runbook symptom row).
- **computes**: readiness = `pg_isready` exec (TCP accepts-then-resets during
  startup); no liveness — a wedged compute is a rude-suspend away from fresh.

## Network boundary

Default-deny ingress + explicit edges (`templates/networkpolicies.yaml`).
Host-facing ports (MCP 8080, compute 55433, pageserver 9898, storcon 1234,
safekeeper 7676) admit all sources because NodePort traffic arrives from the
node address — the host-side guard is the installer's **loopback binding**.
**kind's default CNI does not enforce NetworkPolicy**: on the demo cluster
these are a rendered, CI-checked contract; enforcement requires a
policy-capable CNI (Calico/Cilium), which is part of the real-cluster story,
not M1.

## Namespace ownership

`sspc-cell` is an **operator-owned cell namespace**, not a multi-tenant
boundary: the operator Role spans the namespace's pods/services/configmaps/
secrets by design. Nothing else should be deployed into it. The kubelet
`nodes/proxy` ClusterRole exists solely for basic usage metrics; it reads
stats, never mutates. The check script pins the Role's verb sets so RBAC
growth is a visible review event.

## Availability & quota stance

Single replicas everywhere, no PodDisruptionBudgets, requests-without-limits:
all deliberate for the single-node M1 cell and documented as such. No
availability claim is made or implied until leader election, multi-safekeeper
identity, and pageserver failover land together (M2+, backlog). Before any
shared-cluster target, add ResourceQuota/LimitRange or require a dedicated
namespace with external policy.
