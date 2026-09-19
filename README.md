# Supabricks Platform

[Delivered scope and remaining work](docs/plans/status.md) ·
[Controlled Sail source builds](docs/architecture/source-built-sail.md)

[Project packaging proposal](docs/architecture/project-packaging.md) ·
[Industry research](docs/research/project-packaging-industry.md) ·
[Packaging implementation plan](docs/plans/project-packaging-implementation.md)

Supabricks is a native local database and analytics platform: PostgreSQL 17.8
with branching, Sail/Spark SQL over immutable Delta snapshots, a browser console,
file ingestion, and Jupyter notebooks with managed Python environments. It runs
without Kubernetes, Docker, a system Python, or a model account on the target
machine. Code is [Apache 2.0](LICENSE); bundled components retain their licenses.

Start with the [installed local walkthrough](docs/handbook/local-demo.md). The
[localhost curl installer](install/native/README.md) ships the runtime, compiled
console and synthetic demo files. Public deployment to `supabricks.io` is deferred.
R04 qualifies the combined workflow; see the [release contract and evidence map](docs/architecture/r04-local-release.md).

- [Project inspection preview](docs/handbook/project-inspection.md): offline format-1/2 source validation and resource graphs; packaging/deployment follow later.
- [PostgreSQL workspace](docs/handbook/database-workspace.md): branches, catalog,
  SQL, cancellation and explicitly saved queries.
- [File ingestion](docs/handbook/file-ingestion.md): CSV/TSV, JSON, JSONL and
  Parquet into new PostgreSQL tables through the console, CLI or MCP.
- [Analytical workspace](docs/handbook/analytical-workspace.md): explicit snapshot
  publication, pinned Spark SQL sessions and bounded result comparison.
- [Notebooks](docs/handbook/notebooks.md): local JupyterLab editor, project files,
  Sail-backed kernels and [managed environments](docs/handbook/notebook-environments.md).
- [Recovery](docs/handbook/recovery.md): stopped backups, new-root restore and
  explicit platform upgrades; application source has its own backup.

I03 and C03 are merged and qualified on Linux x86_64 and macOS arm64. N00–N06 and
NE01–NE06 are also merged; their regressions remain mandatory release gates.
The [console/ingestion plan](docs/plans/console-ingestion-implementation.md) tracks
R04 and subsequent optional work. The frontend source lives in
[supabricks/console](https://github.com/supabricks/console), pinned here as the
`console/` submodule. Run `git submodule update --init console` before source
builds; the installed product does not need a checkout or Node.

## Earlier Kubernetes profile

Serverless Postgres on your own Kubernetes: a Rust operator + Helm chart that
turn declarative `Database`/`Branch` resources into disaggregated Postgres
(Neon's Apache-2.0 storage engine) with scale-to-zero, ~1s wakes, instant
copy-on-write branches, TTL self-cleanup — and an MCP server so agents (Claude
Code) are first-class users. Design: [RFC 012](https://github.com/supabricks/rfcs/blob/main/design/012-poc-m1-plan.md).
New to the codebase? Start with the engineering handbook:
`docs/handbook/README.md` (architecture as built, dev loop + landmines,
runbook, and what's deliberately deferred).

## Kubernetes quickstart (laptop, kind)

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

The native Supabricks runtime is maintained alongside this earlier Kubernetes profile;
see the [implementation plan](docs/plans/local-runtime-implementation.md).

- `crates/core` — portable compute configuration, authentication, validation,
  identities and branch decisions. See the [core contract](crates/core/README.md).
- `crates/local` — native PG17 cell and local state daemon: SQLite metadata,
  resumable operations, worktree selection, Process Compose supervision and
  SeaweedFS storage. See [native setup and qualification](e2e/native/README.md)
  and [local state](docs/handbook/local-state.md).
- `crates/operator` — CRDs (`Database`, `Branch`), reconcilers (tenant/timeline
  via the storage controller, compute pods running stock Neon images with
  compute_ctl as PID 1), lifecycle loop (idle-suspend via SQL activity +
  session-churn polling, TTL reaper), Ed25519 compute-auth, and the MCP façade
  (streamable HTTP with the GET/SSE leg, 14 tools — the count is pinned by a
  unit test, and the schema by a snapshot fixture).
- `chart/` — the platform: storage cell (pageserver, safekeeper, broker,
  storage controller + its PG, demo MinIO) + operator + CRDs.
- `install/` — pinned-digest one-command install / teardown.
- `e2e/` — the T3/T4 acceptance suite (drives everything through MCP).

## Connect an agent

The API is standard MCP (streamable HTTP, including the optional GET/SSE
server stream that some clients require) — any MCP-capable harness works.
`up.sh` registers the one it knows:

- **Claude Code**: `claude mcp add -s user -t http sspc http://localhost:30080/mcp`

Any other harness: point its MCP config at the same URL. **Auth default is
open mode**: the installer binds all host ports to loopback, so the network
layer is the guard (this is the deliberate POC posture; real IAM is RFC 008).
To require a bearer instead, set `SSPC_MCP_REQUIRE_TOKEN=true` on the operator
and pass `Authorization: Bearer <token>` with the token from:
`kubectl -n sspc-cell get secret sspc-mcp-token -o jsonpath='{.data.token}' | base64 -d`.

## Honest M1 limits (by design — see RFC 012)

Single admin MCP token; per-endpoint NodePorts (gateway lands in M2, bringing
plain-psql wake-on-connect); one safekeeper; `cloud_admin` credentials; no TLS.
This is the demoable kernel, not the product.
