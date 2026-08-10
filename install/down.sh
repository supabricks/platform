#!/usr/bin/env bash
# Tear down the sspc kind cluster and Claude Code registration.
set -euo pipefail
command -v claude >/dev/null && claude mcp remove -s user sspc >/dev/null 2>&1 || true
for cfg in "$HOME/.mcp-client/mcp.json" "$HOME/.mcp-client/settings/mcp.json"; do
  if [ -f "$cfg" ] && command -v jq >/dev/null; then
    tmp=$(mktemp)
    jq 'del(.mcpServers.sspc)' "$cfg" > "$tmp" && mv "$tmp" "$cfg" || true
  fi
done
kind delete cluster --name sspc
echo "sspc cluster deleted"
