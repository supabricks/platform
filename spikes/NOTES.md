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

