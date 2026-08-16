#!/usr/bin/env bash
# The chart's security contract — implemented as a Rust integration test so
# it needs nothing beyond the repo's required toolchain (cargo + helm).
# This wrapper exists for humans and docs; CI runs it via `cargo test`.
set -euo pipefail
cd "$(dirname "$0")/.."
exec cargo test --locked --test chart_hardening -- --nocapture
