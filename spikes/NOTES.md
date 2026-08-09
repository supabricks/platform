# Spike: manual cell bring-up (004 weeks 1-2)
Copied from reference/neon/docker-compose @ 8f60b04. Bring-up notes below.

## Day 1 (2026-08-07): full cell + CoW branch working locally

**Stack**: stock `ghcr.io/neondatabase/neon:latest` (arm64 native ✓) — MinIO, pageserver,
3 safekeepers, broker, compute (their fork PG 16.9). Brought up with `docker-compose up -d`
(note: brew `docker-compose` needed; colima docker lacks the compose plugin).

**Proven end-to-end**:
- SQL through the full disaggregated path; 100k-row table created and served.
- **Branch creation: 52ms** via one pageserver API call (O(metadata) confirmed live).
- Second compute mounted on the branch via `TENANT_ID`/`TIMELINE_ID` env overrides +
  `docker-compose run -p 55434:55433`; branch saw parent's 100k rows instantly through the
  ancestor read path, diverged to 150k; **parent unchanged**. Layers visible in MinIO (40MiB/6 obj).

**The provisioning flow (operator B1 crib sheet, from `compute_wrapper/shell/compute.sh`)**:
1. Tenant: `PUT pageserver:9898/v1/tenant/{id}/location_config` `{"mode":"AttachedSingle","generation":1,...}`
2. Timeline: `POST /v1/tenant/{id}/timeline/` `{"new_timeline_id","pg_version"}` (+ `ancestor_timeline_id` for branches)
3. Compute: `compute_ctl --pgdata ... -C postgres://cloud_admin@localhost:55433/postgres -b postgres --config <spec.json>`
   Spec template: `compute_wrapper/var/db/postgres/configs/config.json` — placeholders TENANT_ID/TIMELINE_ID;
   key GUCs: `neon.safekeepers`, `neon.pageserver_connstring`, `shared_preload_libraries=neon,...`;
   roles/databases declared in spec; JWKS block for compute_ctl auth.

**Findings for the operator design**:
- `suspend_timeout_seconds` is a NATIVE spec field (−1 = disabled) → compute_ctl may handle
  idle-suspend itself; investigate semantics before building our own idle loop.
- compute_ctl exposes an HTTP API on :3080 (status/metrics) — likely our readiness + activity signal.
- This compose bypasses the storage controller (direct pageserver attach, generation=1).
  The operator must go through the storage controller instead (generations/failover).
- Compute readiness in compose: < ~15s from container start (unmeasured; includes catalog
  basebackup + PG start). K8s timing measurements TBD.

**Next**: suspend/wake experiment via spec + :3080 API → then kind + kubectl + helm install,
translate cell to K8s manifests, storage controller included, measure cold-start distribution.

## Day 2 (2026-08-08): suspend/wake semantics + O12 + O22 — the operator owns everything

**Headline findings for the operator design (B1):**

1. **compute_ctl does NOT self-suspend — the Day-1 hypothesis is falsified.** In current code,
   `spec.suspend_timeout_seconds` only tunes the installed-extensions stats collection interval
   (`compute.rs:2800-2815`). Ran 4+ min idle with `suspend_timeout_seconds: 60`: status stayed
   `running`, 12 PG procs. **The idle loop is entirely ours** (operator/gateway), as 001 assumed.
2. **`last_active` on `/status` is empirically unreliable**: stayed `null` after real queries
   (including a 2s `pg_sleep` + 10s wait). The activity monitor runs (`compute.rs:991`) but short
   sessions never register. **Evidence for 001 §4.3: the gateway must own last-activity truth.**
3. **Suspend = `POST /terminate`** (authed): graceful, returns final flush LSN (`{"lsn":"0/29E2300"}`
   — record it at suspend). Afterwards PG is gone but compute_ctl stays up serving
   `status: "terminated"` — a husk for the control plane to confirm, then delete the pod.
4. **Wake = fresh pod, always.** `docker restart` after an unclean stop crash-looped on a stale
   `/tmp/.s.PGSQL.55433.lock` (fresh container PID collided with the recorded postmaster PID).
   Reused container state is not a wake path; K8s pod-per-wake gets this right for free.
   (Also: compute.sh is PID 1 bash with no signal trap — docker stop = 10s then SIGKILL to PG.)
5. **Wake timing (laptop, colima)**: fresh recreate → SQL-ready-with-data in **12.1s total**, of
   which **10.8s is Docker container-creation overhead** and **~0.6s** is compute_ctl start →
   `running` (basebackup + safekeeper quorum election + PG start; PG "ready" in ~0.5s). The
   engine-side wake is sub-second on a laptop — TR-3's budget is spent in orchestration, not storage.
6. **State-in-cell proven the hard way**: destroyed and recreated the compute container twice;
   150k rows served every time from pageserver+MinIO. Note: `compute.sh` attaches to timeline
   `.[0]` from the pageserver list — with 2 timelines it picked the **branch**, not the parent.
   Timeline discovery by list order is nondeterministic; **the operator must pin tenant+timeline
   explicitly** in the compute spec.

**:3080 auth (compute_ctl external HTTP API) — three inherited bugs found, all fixed locally:**
- Upstream `compute.sh` has a broken line continuation: `--config "${CONFIG_FILE}"` lacks a
  trailing `\`, so **`--dev` never reaches compute_ctl** → API runs in full-JWT mode.
- The README's JWKS recipe is wrong: it base64-encodes the *hex text* of the pubkey (65 bytes,
  with padding + trailing newline). compute_ctl: `Base64 error: Invalid padding` — as shipped,
  **no token can ever verify**. Correct `x` = base64url(raw 32 bytes), no padding.
- **The shipped demo keypair is mismatched**: pubkey derived from `private-key.pem` is
  `98e2e1fc…` but `public-key.der` contains `0d8d1a97…`. Not a pair.
- Working recipe: `mint-compute-jwt.sh` (this dir) — EdDSA JWT, claims
  `{"scope":"compute_ctl:admin","aud":["compute"],"exp":…}`, signed w/ openssl pkeyutl `-rawin`;
  JWK `x` derived from the private key. Verified against `/status`.
- Route inventory (server.rs): `/status`, `/configure` (live spec push), `/terminate`,
  `/metrics(.json)`, `/insights`, `/database_schema`, `/dbs_and_roles`, `/extensions` (install!),
  `/grants`, **`/lfc/offload`** (LFC offload/prewarm state — the TR-3 cache-persistence machinery
  already has an API surface), `/promote`, `/check_writability`.

**O12 CLOSED**: `hypopg` and `pg_hint_plan` both present in stock compute image and functional
(hypothetical index created; `/*+ SeqScan */` hint respected). Also present: pg_stat_statements,
pg_cron, timescaledb. No image blockers for 007's advisor stack.

**O22 ANSWERED**: inherited GetPage throttling = per-tenant **token bucket**
(`pageserver_api/models.rs:1245` — initial/refill_interval/refill_amount/max → steady_rps),
applied at pagestream request level (post-PR #9962; legacy `task_kinds` non-emptiness = enabled
flag), live-settable per tenant via `tenant_conf.timeline_get_throttle`. Maps to our 3 QoS classes
as **per-class rate-cap templates** stamped onto tenant configs — the policy-surface-only build 011
hoped for. Caveat for the adversarial benchmark: these are **static ceilings, not priorities** —
"background defers to production under contention" is NOT inherited; adaptive fairness is our work
if partners demand it.

**Local config changes (uncommitted)**: `config.json` — corrected JWKS (raw-byte x/kid) +
`suspend_timeout_seconds: 60` (harmless; it's a stats interval). Container recreated post-rename —
note: **bind mounts recorded the old `pg-cos-compat` path**; the stack survived the rename only
while running. Any restart needed `--force-recreate` to re-resolve mounts. Host `pgroot/` binaries
also broke (dylib install paths bake the old prefix) — rebuild or symlink when next needed.

**Day 2 addendum — the 10.8s decomposed (and mostly deleted).** The 10.8s "container overhead"
was an artifact of measuring wake via `--force-recreate`, which first *stops* the old husk
container: PID-1 bash ignores SIGTERM → Docker burns its full 10s kill-grace → SIGKILL (measured:
`docker stop` = 10.4s, `rm` = 0.1s, create+start = 0.6s). A true wake never stops anything
(suspend already deleted the compute). Measured clean:
- **Wake (create → SQL-ready-with-data): 0.8–2.0s end-to-end on the laptop** (2.0s cold-ish,
  0.81s warm; includes compute.sh's nc-wait + discovery curls, which the operator replaces with
  direct spec generation).
- Fixed root cause in our compute.sh: `exec compute_ctl` (PID 1, receives SIGTERM directly;
  also deleted upstream's orphaned `--dev` line). After the fix: **`docker stop` = 0.44s**
  (graceful, lock files cleaned — the stale-lock crash-loop class is gone), stop→start cycle
  ≈ 2s. Same lesson for K8s: compute_ctl must be the container entrypoint (or wrapped with
  exec), or every pod deletion eats `terminationGracePeriodSeconds` and kills PG hard.

## Day 3 (2026-08-08): P0 complete — cell on kind, controller in the loop, both de-risks green

**P0 exit criteria all met in one session** (RFC 012 estimated 3–4 days):

- **Cell on kind via `kubectl apply -f spike/k8s/cell/`**: MinIO(demo) + bucket job, broker,
  safekeeper×1, pageserver, storage controller + its PG, all Ready. kind config pre-maps the
  M1 port block (30001–30020 endpoints, 30080 MCP, 30098/30099 debug).
- **Storage controller IS in the loop** (D2 verdict: GO, fallback not needed): launched with
  `--listen/--database-url/--dev`; pageserver self-registers (metadata.json + control_plane_api
  `…/upcall/v1/`; az id required). Tenant + timeline (incl. ancestor branches) created through
  the controller (`POST /v1/tenant`, `POST /v1/tenant/{id}/timeline`).
- **De-risk ① green**: 90-line stdlib stub (spike/mcp-stub/server.py) speaking streamable-HTTP
  MCP + bearer auth; `claude mcp add -t http` → ✔ Connected; headless `claude -p` listed and
  CALLED the tool and returned the URI. Bonus: Claude Code accepts a 405 on the GET/SSE leg —
  **the M1 operator does not need to implement SSE.**
- **De-risk ② found a real landmine**: with `control_plane_url` unset, the controller PANICS
  (`compute_hook.rs:892` unchecked unwrap, "validated at startup" except not in `--dev`) on the
  first compute notification and crash-loops (startup reconcile retries it). P0 workaround: a
  busybox always-200 `notify-sink` + `--control-plane-url`. **M1 design insight: the operator
  should BE this receiver** — notify-attach is the placement-change signal that triggers
  compute-spec regeneration.
- **K8s gotchas recorded**: (a) headless-service pod DNS only exists for READY pods, but
  controller registration resolves the DNS name and readiness depends on registration →
  deadlock; fix `publishNotReadyAddresses: true`. (b) `kind load docker-image` fails on
  multi-arch images under the containerd store; fix `docker save --platform linux/arm64` +
  `kind load image-archive`. (c) compose's `cloud_admin` md5 hash is not "password" — spec
  template now carries our own known hash (`sspc-p0`); external (NodePort) connections hit md5
  auth while in-pod ones hit trust.
- **Provisioning flow scripted** (`spike/k8s/compute/mk-compute.sh` — the operator crib sheet):
  tenant → timeline (± ancestor) via controller → spec ConfigMap (JWKS + tenant/timeline pinned,
  D6/D7) → pod (compute_ctl as PID-1 command, stock compute-node-v16 image, no wrapper) →
  NodePort service.
- **Numbers (kind on colima, laptop)**: first compute pod Ready **1.7s** from apply; branch =
  timeline create + compute + SQL-ready **1.4s wall**; **wake distribution n=5: 1.3–1.8s to
  SQL-verified-with-data** (incl. 1s-quantized readiness probe + exec overhead). 100k rows
  loaded via NodePort from host; branch saw 100k, diverged to 150k, parent untouched.

**Next (P1)**: `platform/` Rust workspace — helm chart wrapping spike/k8s/cell, operator kernel
(Database/Branch CRDs, reconciler = mk-compute.sh flow, JWT minting, golden-spec tests), then
P2 MCP façade. The notify-sink gets replaced by the operator's own notify receiver.

## P1 day 1 (2026-08-08): operator kernel live — CR → psql → branch → clean delete on kind

`platform/` Rust workspace, `sspc-operator` crate (kube v4.2, jsonwebtoken 11, aws-lc-rs).
Ran on the host against the kind cluster (kubeconfig + storcon NodePort) — no image build needed
to validate the reconcilers:

- **Database CR `app1`** → reconciled in ~2.5s: tenant+timeline via storcon (IDs derived
  deterministically from CR UID — replay-safe, no state between attempts), spec ConfigMap
  (golden-tested against the P0 fixture), compute pod (compute_ctl PID 1), NodePort svc
  (stable-hash pick + probing over the label-selected used set). psql via 30004, 50k rows.
- **Branch CR `app1-dev`** → timeline w/ ancestor from parent's status, own compute on 30003;
  saw 50k, diverged to 75k, parent untouched. The kube-native replay of the P0/compose proof.
- **Delete both** → finalizer `sspc.io/cell-cleanup` deleted timeline/tenant cell-side
  (verified: operator's tenant gone from storcon; only P0 manual tenants remain); ConfigMap/
  Pod/Service reaped by ownerReference GC — zero bespoke child-deletion code.
- **T1 tests: 9/9** (golden spec, JWT mint/verify roundtrip vs Day-2 recipe incl. kid
  derivation, port allocator fill/exhaust/stability, deterministic IDs).
- Crypto: jsonwebtoken 11 + kube both need an explicit rustls CryptoProvider once ring and
  aws-lc-rs coexist → standardized on **aws-lc-rs** end-to-end (the 006-O7 FIPS-aligned pick),
  `install_default()` at main. Fixture note: P0 golden updated to the sspc-p0 password hash.
- CRDs generated by `cargo run --bin crdgen` → `platform/chart/crds/sspc-crds.yaml` (applied).

**P1 remainder**: Helm chart (parameterize cell manifests — namespace is baked into DNS names
in several places incl. the Rust spec template; needs a values-driven render), operator
Dockerfile + in-cluster Deployment/RBAC, then the P1 exit run (`helm install` → CR → psql).

## P1 COMPLETE (2026-08-09): helm install → CR → psql → clean delete, all in-cluster

- **Spec render de-namespaced**: SAFEKEEPERS_ADDR / PAGESERVER_CONNSTRING placeholders;
  operator derives defaults from its namespace (env-overridable); golden test still
  byte-exact vs the P0 fixture.
- **Chart** (`platform/chart/`): 22 resources — cell (minio demo toggle, broker, safekeeper,
  pageserver, storcon+pg, notify-sink) + operator (SA/Role/RoleBinding/Deployment, namespace
  via downward API) + CRDs in `crds/`. Values: images, pull policy, s3.*, safekeeper.replicas
  (=1 only; 3-SK toggle needs ordinal-derived --id — chart TODO), NodePorts. Lints clean.
- **Operator image**: `platform/Dockerfile` (rust:1.92 build stage + cmake/perl for aws-lc-rs;
  bookworm-slim runtime, nonroot). `sspc-operator:p1` kind-loaded via the save --platform trick.
- **Exit run from a deleted namespace**: `helm install sspc platform/chart -n sspc-cell
  --create-namespace` → **all pods Ready in 14s** (operator in-cluster). Database CR `exit1` →
  **pod Ready in 3.4s**, psql via NodePort 30018, 10k rows. Branch CR → inherited on 30019.
  Deletes → **tenants: 0, endpoint objects: 0** (finalizer + ownerRef GC, verified in storcon).
- Gotcha for the e2e script: `kubectl delete branch X database Y` parses as two *branches* —
  delete kinds separately.

**Next (P2)**: MCP façade in the operator (streamable HTTP, the 9 tools, bearer auth,
idempotency keys) — de-risk ① already proved the client side; then `claude mcp add` against
the real thing = the M1 core demo moment. Then P3 lifecycle (idle-suspend via SQL poll,
wake in get_connection, TTL reaper).

## P2 (2026-08-09): the core demo moment — Claude Code provisions on the real platform

- **MCP façade** (`src/mcp.rs`, ~450 lines): hand-rolled streamable-HTTP JSON-RPC (D8's
  sanctioned fallback — de-risk ① proved the exact surface: POST-only, 202 for notifications,
  405 on GET/SSE). 9 tools as thin verbs over the CR model; create is server-side apply on
  name → naturally idempotent; bounded 30s await-ready with an honest "provisioning, poll
  get_connection" fallback; structured errors {reason, retriable, suggested_action}.
- **Auth**: bearer token minted into Secret `sspc-mcp-token` on first run (aws-lc-rs random);
  Service `sspc-mcp` NodePort 30080 (already in the kind port map). 401 without it, verified.
- **THE TEST**: `claude mcp add -t http :30080` → ✔ Connected → headless `claude -p` session:
  created `agentdb` (URI returned), loaded 1,000 rows via dockerized psql ITSELF, branched to
  `agentdb-test`, verified the branch sees all rows. Demo script steps 1–3 are live minus the
  installer — a real agent, zero humans, zero kubectl.
- **The agent found a real bug**: operator Role lacked create/delete on databases/branches
  (the in-operator MCP server creates CRs itself — my host-run P1 validation used my admin
  kubeconfig, masking it). Chart fixed (+create,+delete), helm rev 3, verbs verified. Lesson
  for T4/T5: run e2e under the operator's RBAC, never an admin identity.
- Live artifacts left running: `agentdb` (:30018) + `agentdb-test` (:30004); MCP registration
  in Claude Code local scope for the platform/ dir — open Claude Code there and ask for a
  database.

**Next (P3)**: lifecycle — idle-suspend loop (SQL activity poll → /terminate w/ minted JWT →
delete pod, port sticky), wake inside get_connection (fresh pod), TTL reaper + K8s Events;
suspend/wake status phases. Then P4: up.sh installer + `just e2e` (T3/T4) + pinned digests.

## P3 (2026-08-09): scale-to-zero, wake, and TTL — all live

- **Idle-suspend** (`src/lifecycle.rs`, 15s tick): SQL activity poll per Active endpoint →
  Day-2 terminate sequence (minted JWT → `POST /terminate` → flush LSN into status → delete
  pod; Service stays = sticky port). Suspend-awareness in the reconciler via monotonic
  timestamps: run unless `phase==Suspended` and no `sspc.io/wake-requested-at` annotation
  newer than `suspendedAt` — no annotation clearing, unit-tested.
- **THE TRAP, REPRODUCED**: first run never suspended — `compute_ctl:compute_monitor` holds a
  persistent client-backend session, i.e. the research doc's Neon-cloud `check_availability`
  "never truly zero" problem, found in our own living room. Filter now excludes
  `application_name LIKE 'compute_ctl%'` and `sspc-operator`. M2's gateway-owned activity
  truth remains the real fix.
- **Measured**: `sleepy` (suspendAfter=30s) → **Suspended after 38s idle**, pod gone, LSN
  `0/17261D0` recorded. `get_connection` → wake annotation → reconciler recreates pod →
  **1.3s to URI**, same port, 20k rows intact.
- **TTL reaper**: 10 branches of one parent burst-created concurrently via MCP — **all ready
  in 7s wall** — then all 10 reaped on schedule (60s TTL), 10 `TTLExpired` Events (the audit
  line), 0 pods left, tenant timeline count back to 1 (cell-side deletion verified).
- CRD additions: `Branch.suspendAfterSeconds`, `status.suspendedAt` (crdgen re-applied —
  remember helm won't upgrade crds/).
- Demo-script status: **steps 2–5 all live** (wake is MCP-explicit; plain-psql wake = M2).
  Remaining for M1: P4 — up.sh installer, `just e2e` under operator RBAC, pinned digests.

