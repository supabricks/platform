#!/usr/bin/env bash
# sspc one-command install (RFC 012 B0): kind cluster + platform + MCP
# registration. Usage: up.sh [--yes]   (--yes = no prompts, CI mode)
set -euo pipefail
cd "$(dirname "$0")"
YES=${1:-}

# ---- image pins: derived from chart/values.yaml — the chart's digest
# fields are the single source of truth; this parser reads the one-line
# `key: {name: "...", digest: "..."}` entries.
PINS=()
while IFS= read -r line; do
  name=$(printf '%s' "$line" | sed -E 's/.*name: "([^"]+)".*/\1/')
  digest=$(printf '%s' "$line" | sed -E 's/.*digest: "([^"]+)".*/\1/')
  [ "$digest" = "local-build" ] && continue
  PINS+=("$digest|$name")
done < <(grep -E '^\s+\w+: \{name: "' ../chart/values.yaml)
[ "${#PINS[@]}" -ge 6 ] || { echo "FATAL: could not parse image pins from chart/values.yaml" >&2; exit 1; }
OPERATOR_TAG=$(grep -E 'operator: \{name: "' ../chart/values.yaml | sed -E 's/.*name: "([^"]+)".*/\1/')

say() { printf '\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

say "checking prerequisites"
for bin in docker kind kubectl helm jq; do
  command -v "$bin" >/dev/null || die "$bin not found — install it first"
done
docker info >/dev/null 2>&1 || die "docker daemon not running"

if ! kind get clusters 2>/dev/null | grep -qx sspc; then
  say "creating kind cluster 'sspc' (pre-mapped port block 30001-30020, 30080)"
  kind create cluster --config kind-config.yaml --wait 120s
else
  say "kind cluster 'sspc' already exists"
fi

say "pulling pinned images + loading into kind (first run: a few minutes)"
tags=()
for pin in "${PINS[@]}"; do
  digest="${pin%%|*}"; tag="${pin##*|}"
  docker image inspect "$tag" >/dev/null 2>&1 || {
    docker pull -q "$digest"
    docker tag "$digest" "$tag"
  }
  tags+=("$tag")
done
docker image inspect "$OPERATOR_TAG" >/dev/null 2>&1 || {
  say "building operator image (not found locally)"
  docker build -t "$OPERATOR_TAG" ..
}
# The load-skip must compare IMAGE IDS, not just names: a same-tag operator
# rebuild would otherwise silently exercise the stale node image. (The
# compute-image presence check guards against node-side prunes while every
# database is suspended — the "unused" compute image gets reaped.)
local_op_id=$(docker image inspect "$OPERATOR_TAG" --format '{{.Id}}' 2>/dev/null || true)
node_op_id=$(docker exec sspc-control-plane crictl inspecti -o go-template --template '{{.status.id}}' "docker.io/library/$OPERATOR_TAG" 2>/dev/null || true)
if [ -n "$local_op_id" ] && [ "$local_op_id" = "$node_op_id" ] \
   && docker exec sspc-control-plane crictl images 2>/dev/null | grep -q compute-node-v16; then
  say "images already on the node (operator image ID matches); skipping load"
else
  tar=$(mktemp -d)/images.tar
  docker save --platform linux/arm64 -o "$tar" "${tags[@]}" "$OPERATOR_TAG" \
    || docker save -o "$tar" "${tags[@]}" "$OPERATOR_TAG"
  kind load image-archive --name sspc "$tar" >/dev/null
  rm -f "$tar"
fi

say "installing the platform (helm)"
# helm only installs crds/ on first install; re-apply so upgrades get schema
# changes too (RFC 014 PR A: Branch gained parent/at).
kubectl apply -f ../chart/crds/ >/dev/null
helm upgrade --install sspc ../chart -n sspc-cell --create-namespace >/dev/null
# Wait on workloads, not pods --all: the bucket job's Completed pod is never
# "Ready" and would hang the wait (found by the second-consecutive-run test).
kubectl -n sspc-cell wait --for=condition=Available deploy --all --timeout=300s >/dev/null
for ss in controller-pg safekeeper pageserver; do
  kubectl -n sspc-cell rollout status "statefulset/$ss" --timeout=300s >/dev/null
done
kubectl -n sspc-cell wait --for=condition=Complete job/minio-create-bucket --timeout=120s >/dev/null 2>&1 || true
say "platform healthy"

MCP_URL=http://localhost:30080/mcp

say "smoke test: create + delete a database through MCP"
mcp() { curl -sf -X POST -H "Content-Type: application/json" -d "$1" "$MCP_URL"; }
r=$(mcp '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"create_database","arguments":{"name":"smoke-check"}}}' | jq -r '.result.content[0].text' | jq -r .status)
[ "$r" = "ready" ] || die "smoke create returned '$r'"
mcp '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"delete_database","arguments":{"name":"smoke-check"}}}' >/dev/null
say "smoke test passed"

ADD_CMD="claude mcp add -s user -t http sspc $MCP_URL"
if command -v claude >/dev/null; then
  consent=y
  if [ "$YES" != "--yes" ]; then
    read -r -p "Register the sspc MCP server with Claude Code (user scope)? [y/N] " consent
  fi
  if [ "$consent" = "y" ] || [ "$consent" = "Y" ]; then
    claude mcp remove -s user sspc >/dev/null 2>&1 || true
    eval "$ADD_CMD" >/dev/null
    say "registered with Claude Code — open a session here and ask for a database"
  else
    say "skipped; register later with:"; echo "  $ADD_CMD"
  fi
else
  say "claude CLI not found; register later with:"; echo "  $ADD_CMD"
fi

# third-party MCP clients: reads mcpServers from ~/.mcp-client/mcp.json (global) or
# mcp-client/mcp.json (project). Our server speaks full streamable HTTP including
# the GET/SSE server stream (the MCP client requires it; Claude Code doesn't).
# the client's docs say ~/.mcp-client/mcp.json; real installs use ~/.mcp-client/settings/mcp.json.
# Write both (idempotent merges) so either version picks it up.
CLIENT_CFGS="$HOME/.mcp-client/mcp.json"
[ -d "$HOME/.mcp-client/settings" ] && CLIENT_CFGS="$CLIENT_CFGS $HOME/.mcp-client/settings/mcp.json"
if [ -d "$HOME/.mcp-client" ]; then
  consent=y
  if [ "$YES" != "--yes" ]; then
    read -r -p "Register the sspc MCP server with third-party MCP client (~/.mcp-client/mcp.json)? [y/N] " consent
  fi
  if [ "$consent" = "y" ] || [ "$consent" = "Y" ]; then
    for cfg in $CLIENT_CFGS; do
      [ -f "$cfg" ] || printf '{"mcpServers":{}}\n' > "$cfg"
      tmp=$(mktemp)
      jq --arg url "$MCP_URL" \
        '.mcpServers.sspc = {"type": "streamable-http", "url": $url,
                              "alwaysAllow": [], "disabled": false}' \
        "$cfg" > "$tmp" && mv "$tmp" "$cfg"
    done
    say "registered with third-party MCP client — restart the server from the client's MCP settings to pick it up"
  else
    say "skipped third-party MCP client registration"
  fi
fi

[ "$YES" != "--yes" ] && command -v open >/dev/null && open "http://localhost:30080/" || true
cat <<EOF

  sspc is up.
    UI:       http://localhost:30080/
    try:      open Claude Code (or third-party MCP client) and say "create me a postgres database"
    inspect:  kubectl -n sspc-cell get databases,branches,pods
    e2e:      just e2e        teardown: ./down.sh
    other harnesses: any MCP client works — see platform/README.md "Connect an agent"
EOF
