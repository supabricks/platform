# Deferred work and known debt

Two lists. The first is *strategy* — decided scope, do not build without a
ratified RFC addendum. The second is *debt* — agreed-imperfect implementation
detail, fair game when it blocks you, each with its exit condition.

## Deliberately deferred (the M2+ line — RFC 004 addendum v2, RFC 014 §3)

| What | Why deferred | Where designed |
|---|---|---|
| **Gateway / wake-on-connect** (single entry port, plain-psql wake, exact activity truth, deletes the NodePort block + SQL idle polling) | least de-risked component; M1 didn't need it to demo; sequenced by discovery interviews | 004 B2, 012 D3/D4 |
| **TLS** | POC posture is loopback-bound; TLS belongs with the gateway's front door | 006 |
| **OAuth/OIDC/IAM, RBAC, agent identity** | open mode + loopback is the deliberate POC stance ("we'll implement oauth properly in the real one") | 008, 006 |
| **HA**: 3 safekeepers (values toggle exists, untested), pageserver failover, operator leader election | single-replica-everything is the M1 simplification; chaos drills prove single-node recovery, not node loss | 004, 002 |
| **Density-500, perf gates, pgbench collision benchmarks** | the 96-core/190-DB oversubscription test wants real metal | 002, 011 |
| **Metering/billing, multi-cell, federation** | post-POC product surface | 001, VISION |
| **PG fork work** (18.4 rebase, PC-/EC- ledgers) | engine is stock upstream Neon by decision; fork inception is post-POC | 003, 004 |

## Known debt (fix when it blocks you; keep the exit condition honest)

1. **notify-sink**: busybox HTTP 200-sink absorbing storcon compute
   notifications. Exit: operator implements the notify receiver, sink deleted.
2. **Operator is a single writer with no lease**: SIGTERM handler closes the
   rollout race, but two replicas would corrupt. Exit: kube leader election
   before any HA story.
3. **Legacy shared-password fallback**: `endpoint_password()` falls back to
   `SSPC_PG_PASSWORD` for pre-H3 endpoints. Exit: after one estate-wide
   suspend/wake cycle, make the fallback a hard error.
4. **`at` with a bad LSN retries forever**: timestamp resolution fails closed
   with a `Failed` phase + message, but a syntactically-valid-yet-bogus LSN
   only errors at `create_timeline` → reconciler retry loop (MCP shows
   `provisioning`). Exit: classify storcon 4xx as terminal → `Failed`.
5. **Spec ConfigMap changes don't reach running pods** until the next wake
   (compute_ctl `/configure` unused). Harmless today (only the credential and
   pins live there); matters the day specs carry tunables. Exit: wire
   `/configure` or document per-field.
6. **Metrics are in-operator-memory only** (40 samples / 10 min, lost on
   restart). Fine for the estate UI; not a monitoring system. Exit: RFC 007
   Performance Hub owns real telemetry.
7. **NodePort block = hard 20-endpoint cap** and ports are part of the kind
   cluster config (recreation to change). Exit: deleted wholesale by the M2
   gateway.
8. **Docker build has no cargo dependency layer**: every source change
   recompiles all deps (~10 min cold). CI mitigates with a buildx layer
   cache; laptops eat it. Exit: cargo-chef or a deps-first COPY layering.
9. **Events are create-only with timestamp names**: no dedup/aggregation;
   noisy under long retry loops. Exit: switch to EventSeries semantics when
   someone actually consumes events programmatically.
10. **Restore-from-bucket-only is unproven**: S3 + CRs are *designed* as the
    durable truth (P1–P6), and chaos proves node-reboot recovery — but no
    test yet destroys the PVCs and rebuilds a cell from the bucket. Exit:
    the P1 cold-attach verify job (002), post-M1.5.
11. **`safekeeper.replicas` toggle exists but 3-SK needs ordinal-derived
    `--id`/`--advertise-pg`** (chart TODO noted in the template). Exit: with
    the HA story.
12. **e2e asserts through `kubectl exec`** (loopback binding blocks
    host-side psql; runners lack psql). Fine, but it means client-driver
    behavior over NodePort isn't exercised in CI. Exit: gateway conformance
    suite (M2).
