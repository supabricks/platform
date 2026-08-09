#!/usr/bin/env bash
# Tear down the sspc kind cluster and Claude Code registration.
set -euo pipefail
command -v claude >/dev/null && claude mcp remove -s user sspc >/dev/null 2>&1 || true
if [ -f "$HOME/.mcp-client/mcp.json" ] && command -v jq >/dev/null; then
  tmp=$(mktemp)
  jq 'del(.mcpServers.sspc)' "$HOME/.mcp-client/mcp.json" > "$tmp" && mv "$tmp" "$HOME/.mcp-client/mcp.json" || true
fi
kind delete cluster --name sspc
echo "sspc cluster deleted"
