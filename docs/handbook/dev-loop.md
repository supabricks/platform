# The dev loop — and its landmines

## Build / test / deploy a change

```sh
cd platform
cargo test                                  # T1 unit + T2 schema snapshots, <1 min
docker build -t sspc-operator:p1 .          # builds UI + operator (release)
docker save --platform linux/arm64 -o /tmp/op.tar sspc-operator:p1 \
  || docker save -o /tmp/op.tar sspc-operator:p1
kind load image-archive --name sspc /tmp/op.tar
kubectl apply -f chart/crds/                # only if crd.rs changed (see below)
helm upgrade --install sspc chart -n sspc-cell   # only if chart/ changed
kubectl -n sspc-cell rollout restart deploy/sspc-operator
kubectl -n sspc-cell rollout status deploy/sspc-operator
./e2e/run.sh && ./e2e/chaos.sh              # the gate; CI runs the same
```

Changed the MCP tool schema on purpose? `UPDATE_SNAPSHOTS=1 cargo test`,
review the `mcp-tools.json` diff, commit it.

Brew-installed docker CLI (colima) warns `DEPRECATED: legacy builder`?
Install the missing plugin once: `brew install docker-buildx && mkdir -p
~/.docker/cli-plugins && ln -sfn "$(brew --prefix)/opt/docker-buildx/bin/docker-buildx"
~/.docker/cli-plugins/docker-buildx` — builds then use BuildKit (better
layer caching; `-q` still prints the image sha).

Changed `crd.rs`? Regenerate + apply: `cargo run --bin crdgen >
chart/crds/sspc-crds.yaml && kubectl apply -f chart/crds/`. Helm installs
`crds/` **only on first install** — upgrades silently skip it (the installer
re-applies them for this reason).

## The landmines (each of these cost real time — believe the list)

1. **kind + multi-arch images**: `kind load docker-image` fails or loads the
   wrong arch for multi-arch tags on Apple Silicon. Always
   `docker save --platform linux/arm64` → `kind load image-archive` (with the
   plain-save fallback for CI's amd64 runners).
2. **Verify the deploy by behavior, not by grep.** The image sha is the only
   string worth checking (`docker build -q` prints it; identical sha = your
   change didn't make it in — which happens, see #3). Never chain
   `grep && deploy`: our grep is aliased to ugrep, lies about binaries, and a
   failed grep once silently aborted the `kind load` while everything looked
   deployed. A day was lost.
3. **Silent patch failures**: an Edit/str-replace that misses its anchor
   no-ops. If a "fixed" build produces the identical image sha, the source
   didn't change. Re-check the edit landed before debugging the runtime.
4. **PID 1 ignores SIGTERM** — twice now. Compute pods: `compute_ctl` must be
   the container command (a bash wrapper eats SIGTERM → 10s kill grace →
   stale locks). The operator itself: without an explicit SIGTERM handler it
   survived every rollout for the 30s grace period **still reconciling** — a
   zombie second writer that raced its replacement (found when it rendered a
   stale compute spec 3s before the new operator's first pass). The handler
   is in `main.rs`; don't remove it, and don't add long blocking work to
   drop paths.
5. **compute_ctl never goes idle by itself**: it holds a permanent SQL
   session (`compute_ctl:compute_monitor`). Idle detection must exclude
   `application_name LIKE 'compute_ctl%'` or nothing ever suspends — the
   classic "never truly zero" trap.
6. **Branch-at-head races ingestion**: the pageserver ingests WAL
   asynchronously; branching at "now" without the bounded flush-LSN wait
   yields branches missing the parent's latest rows ("relation t does not
   exist"). If you touch `apply_branch`, keep the wait **fail-closed**.
7. **TCP readiness ≠ Postgres readiness**: PG briefly accepts and resets
   connections during startup. Compute readiness is an exec `pg_isready`
   probe; clients still need one retry.
8. **Loopback binding is cluster-create-time**: host ports bind 127.0.0.1 via
   `extraPortMappings.listenAddress` in the kind config — changing it means
   recreating the cluster. Consequence: containers can't reach host-mapped
   ports, so e2e runs psql via `kubectl exec`, not from the host.
9. **The 2-core budget**: cell requests ~0.6 core, kube-system ~0.9; each
   standard-priority endpoint requests `cuLimit×10m`. Five default endpoints
   exhaust a 2-core kind node and pods go Pending with "Insufficient cpu" —
   e2e proof-branches run `cu_limit: 2` for this reason. GitHub runners are
   also 2-core.
10. **Docker context and disk**: `.dockerignore` must exclude `target/`
    (8.2GB — shipping it once filled colima's disk and broke builds with
    "no space left on device"). On colima, a full disk makes `docker build`
    silently no-op; `docker system df` + prune. CI runners need the
    free-disk step in `ci.yml` for the same reason.
11. **`ui/dist` must exist** for `cargo test`/`cargo check` (rust-embed
    compile-time folder check) — a `.gitkeep` pins it. The real dist is
    built inside the Docker image; never commit build output.
12. **helm `--wait` style waits**: wait on Deployments + StatefulSet
    rollouts, never `pod --all` — the bucket-seed Job's Completed pod is
    never Ready and hangs the wait.
13. **Storage controller `--dev` panics** on compute notifications unless
    `control_plane_url` is set — that's what notify-sink is for. Don't
    remove it until the operator implements the receiver.
14. **Some MCP clients need the GET/SSE leg** of streamable HTTP and treat a
    missing stream as a dead server; Claude Code needs neither. Test MCP
    changes against more than one harness.

## Testing tiers

Two aggregate gates mirror CI exactly — when in doubt, run these:

| Gate | Command | Time | Mirrors |
|---|---|---|---|
| static (fmt, unit+snapshots, hardening contract, CRD drift, helm lint, UI tests+build) | `just verify-static` | ~2 min | the CI unit job |
| runtime (e2e + chaos + restore) | `just verify-runtime` | ~8 min | the CI e2e job's test stages |

Individual targets: `just test`, `just hardening`, `just crd-check`,
`just helm-lint`, `just ui-test`, `just e2e`, `just chaos`, `just restore`.
Repeat deploys go through `just deploy` (build → image-archive load →
restart) — the installer's load-skip compares operator image IDs, but the
dev loop should not depend on the installer at all.

`e2e/run.sh` asserts the RFC 014 core-semantics promises by name (credential
distinctness/enforcement, branch-at-LSN and at-timestamp correctness,
branch-of-branch parentage, delete-guard refusals). Treat a failing step as a
regression in a *promise*, not a flaky test: the two times a step "flaked" it
had found a real race (ingestion lag; the PID-1 zombie writer).
