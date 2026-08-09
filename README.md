# sspc platform (M1)

Serverless Postgres on your own Kubernetes: a Rust operator + Helm chart that
turn declarative `Database`/`Branch` resources into disaggregated Postgres
(Neon's Apache-2.0 storage engine) with scale-to-zero, ~1s wakes, instant
copy-on-write branches, TTL self-cleanup — and an MCP server so agents (Claude
Code) are first-class users. Design: `docs/design/012-poc-m1-plan.md`.

## Quickstart (laptop, kind)

Prereqs: docker, kind, kubectl, helm, jq — and the `claude` CLI if you want
the MCP registration.

```sh
./install/up.sh        # cluster + platform + smoke test + claude mcp add (~5 min first run)
```

Then open Claude Code anywhere in this repo and say *"create me a postgres
database and load some test data"*. Or drive it by hand:

```sh
kubectl -n sspc-cell get databases,branches   # the estate
just e2e                                      # the full acceptance suite
./install/down.sh                             # teardown
```

## What's here

- `crates/operator` — CRDs (`Database`, `Branch`), reconcilers (tenant/timeline
  via the storage controller, compute pods running stock Neon images with
  compute_ctl as PID 1), lifecycle loop (idle-suspend via SQL activity polling,
  TTL reaper), Ed25519 compute-auth, and the MCP façade (streamable HTTP,
  bearer token, 9 tools).
- `chart/` — the platform: storage cell (pageserver, safekeeper, broker,
  storage controller + its PG, demo MinIO) + operator + CRDs.
- `install/` — pinned-digest one-command install / teardown.
- `e2e/` — the T3/T4 acceptance suite (drives everything through MCP).

## Honest M1 limits (by design — see RFC 012)

Single admin MCP token; per-endpoint NodePorts (gateway lands in M2, bringing
plain-psql wake-on-connect); one safekeeper; `cloud_admin` credentials; no TLS.
This is the demoable kernel, not the product.
