# Runbook: failure modes and fire drills

First moves for anything wrong:

```sh
kubectl -n sspc-cell get databases,branches,pods         # phases at a glance
kubectl -n sspc-cell logs deploy/sspc-operator --tail 100
kubectl -n sspc-cell get events --sort-by=.lastTimestamp | tail -20
curl -s localhost:30099/debug/v1/tenant | jq              # storcon's view of tenants
```

The design premise: reconcilers are idempotent and IDs are deterministic, so
for almost everything the answer is *wait one reconcile/tick (≤15s) or delete
the pod and let it converge*. If a resource is wedged longer than a minute,
the logs name the reason — the operator warns loudly on every guarded path.

## The three fire drills (T6, `e2e/chaos.sh` — verified behavior, not aspiration)

Run them any time; they are safe by design and CI runs them on every PR.

### Drill 1 — kill the operator mid-lifecycle
```sh
kubectl -n sspc-cell delete pod -l app=sspc-operator
```
**Observed**: replacement pod Ready in ~5s; in-flight lifecycle work resumes
from CR state (an idle database whose suspend was due completed it ~15–30s
after the kill in the drill run); wake afterward returns the data intact.
Nothing to clean up. If suspend/wake stalls >2 min after an operator restart,
look for a panic loop in the logs — state is in the CRs, so `rollout restart`
is always safe to try again.

### Drill 2 — restart the pageserver under load
```sh
kubectl -n sspc-cell rollout restart statefulset/pageserver
```
**Observed**: rollout completes in ~20s (PVC persists; the pageserver
re-registers with the storage controller and re-attaches tenants); active
computes reconnect by themselves — reads recovered in <10s in the drill run,
then writes. Client-side you may see one failed query during the window;
compute pods do NOT restart. If reads don't recover in ~60s check
`curl localhost:30099/debug/v1/tenant` — tenants should list as attached.

### Drill 3 — reboot the node
```sh
docker restart sspc-control-plane
```
**Observed**: apiserver back in ~30s; cell Deployments/StatefulSets
reconverge unattended (~2 min local, similar on CI runners); kubelet restarts
the compute pods that existed (actives return Ready with data intact);
suspended databases stay suspended and wake on demand; MCP/UI answer as soon
as the operator pod is Ready. Total convergence observed: ~130s. Nothing to
clean up. If a compute pod stays Pending afterward, it's the 2-core request
budget (see dev-loop landmine #9), not the reboot.

### Drill 4 — restore the cell from the bucket alone
```sh
./e2e/restore.sh   # quiesce → flush-verify → DESTROY pageserver/safekeeper/controller PVCs → rebuild → verify
```
**Observed** (T7, review 002 P0): flush-to-bucket completes within seconds —
because the chart pins the cell's tenant `checkpoint_timeout` to `10 s`
(stock Neon is 10 *minutes*, which would leave a quiet database's tail WAL
out of the bucket that long; this drill found that). Rebuild from empty PVCs
takes ~16s: every tenant re-attaches from its remote index, suspended
endpoints stay suspended, first wake ~2s, parent and divergent branch serve
their exact row counts, writes work. Full drill: 95s. Estate-wide by nature —
every tenant in the cell re-attaches.

**The one manual-recovery rule it encodes**: if you ever replace
`controller-pg`'s storage, you MUST restart the `storage-controller`
deployment — it runs schema migrations only at startup and caches node state
in memory, so against a fresh database it 500s every pageserver re-attach
with `relation "nodes" does not exist` (found by this drill's first destroy).

## Specific symptoms

| Symptom | Cause | Fix |
|---|---|---|
| `create_database` returns `provisioning` forever | reconcile erroring; or NodePort block exhausted (20-endpoint M1 cap) | operator logs; delete idle endpoints or raise the block (kind config remap = cluster recreation) |
| Database never suspends | a real client is connected; or you added a poller without excluding its `application_name` | check `pg_stat_activity` for `client backend`s; the operator + compute_ctl are excluded already |
| Wake >5s or times out | image pull (must be `Never` + preloaded); cell unhealthy | `kubectl describe pod <name>`; drill-2 checks |
| Branch shows `Failed` phase | bad `at` branch point (timestamp outside parent history) — this is fail-loud by design | `get_database`/status message has the reason; delete and recreate with a valid point |
| Branch creation slow / held at `Provisioning` | ingestion wait against a lagging pageserver — the branch is HELD, never cut early (fail-closed by design) | `status.message` says "ingestion lagging (ingested X, parent flushed Y)"; if it persists, the pageserver is unhealthy — drill-2 checks |
| `delete_database` refuses | it has live branches — H1 guard, not a bug | delete the named branches first |
| "password authentication failed" on a URI that just worked | you're holding a pre-H3 URI, or the pod predates its credential Secret | `get_connection` again (fresh URI); suspend/wake cycles the pod onto its Secret |
| MCP 401 | `SSPC_MCP_REQUIRE_TOKEN=true` mode | token: `kubectl -n sspc-cell get secret sspc-mcp-token -o jsonpath='{.data.token}' \| base64 -d` |
| the MCP client doesn't see sspc | wrong config path or missing GET leg (both fixed — regression check) | `~/.mcp-client/settings/mcp.json` must list it; `curl -N localhost:30080/mcp` must hold an SSE stream open |
| storage-controller CrashLoop | it panicked on a compute notification — notify-sink missing/broken | `kubectl -n sspc-cell get svc notify-sink`; restore it |
| Everything Pending after adding endpoints | request budget exhausted on small nodes | lower `cuLimit`s / delete endpoints; on real metal raise `cellResources` and node counts |

## Recovery invariants (what you can always rely on)

- Deleting a compute pod is always safe: it's a rude suspend; state is in the
  cell; the reconciler recreates on demand (wake) — never reuse stale state.
- Deleting the operator pod is always safe: single writer, deterministic IDs,
  CR-held state. (Scaling it to >1 is NOT safe — no leader election yet.)
- `helm upgrade` + `kubectl apply -f chart/crds/` is always safe: SSA
  converges; cell StatefulSets keep their PVCs.
- The S3 bucket + CRs are the durable truth; pods are cattle. (Full
  restore-from-bucket-only is designed (P1–P6 portability invariants) but not
  yet exercised — see backlog before promising it to anyone.)
