#!/usr/bin/env bash
# Tear down the sspc kind cluster and Claude Code registration.
set -euo pipefail
command -v claude >/dev/null && claude mcp remove -s user sspc >/dev/null 2>&1 || true
kind delete cluster --name sspc
echo "sspc cluster deleted"
