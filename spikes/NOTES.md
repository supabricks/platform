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

**Next**: kind + kubectl + helm — translate the cell to K8s manifests (storage controller included
this time), measure the cold-start distribution (the ~10.8s container overhead is the number K8s
must beat), prototype the operator's suspend→terminate→delete-pod / wake→create-pod loop.

