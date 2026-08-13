#!/usr/bin/env bash
# T6 chaos-lite (RFC 014 H4): the core promises hold across the deaths this
# platform will actually die — operator crash, storage restart, node reboot.
# Assumes the platform is up (install/up.sh). Deliberately uses the same
# public surface as run.sh: MCP for verbs, kubectl for read-only asserts.
set -euo pipefail
cd "$(dirname "$0")"
NS=sspc-cell
ART=./e2e-artifacts
T_START=$(date +%s)

TOKEN=$(kubectl -n $NS get secret sspc-mcp-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
mcp() { # $1 tool, $2 args-json → tool text payload
  curl -sf -m 60 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
    http://localhost:30080/mcp | jq -r '.result.content[0].text'
}
psql_run() { # in-pod psql; retry x3 (pod may be mid-restart during drills)
  for a in 1 2 3 4 5; do
    out=$(kubectl -n $NS exec "$1" -- psql -U cloud_admin -h localhost -p 55433 -d postgres -Atc "$2" 2>&1) && { echo "$out"; return 0; }
    sleep 3
  done
  echo "$out"; return 1
}
step() { printf '\033[1;35m[%3ds] %s\033[0m\n' "$(($(date +%s)-T_START))" "$*"; }
fail() {
  printf '\033[1;31mCHAOS FAIL: %s\033[0m\n' "$*" >&2
  mkdir -p $ART
  kubectl -n $NS logs deploy/sspc-operator --tail 200 > $ART/chaos-operator.log 2>&1 || true
  kubectl -n $NS get databases,branches,pods,svc -o yaml > $ART/chaos-resources.yaml 2>&1 || true
  echo "artifacts in $ART" >&2
  exit 1
}
trap 'fail "unexpected error at line $LINENO"' ERR

step "pre-clean (idempotent)"
for r in e2ec1 e2ec2; do mcp delete_database "{\"name\":\"$r\"}" >/dev/null 2>&1 || true; done
sleep 3

step "drill 1: operator killed mid-lifecycle — suspend still completes"
mcp create_database '{"name":"e2ec1","suspend_after_seconds":15,"cu_limit":2}' | jq -e '.status == "ready"' >/dev/null || fail "e2ec1 not ready"
psql_run e2ec1 "create table c as select 1 as v; select count(*) from c" >/dev/null
# Kill the operator NOW, while e2ec1's idle clock is running.
kubectl -n $NS delete pod -l app=sspc-operator --wait=false >/dev/null
kubectl -n $NS rollout status deploy/sspc-operator --timeout=120s >/dev/null
for i in $(seq 1 30); do
  [ "$(kubectl -n $NS get database e2ec1 -o jsonpath='{.status.phase}')" = "Suspended" ] && break
  sleep 4; [ "$i" = 30 ] && fail "suspend never completed after operator restart"
done
for i in $(seq 1 10); do
  [ -z "$(kubectl -n $NS get pod e2ec1 --no-headers --ignore-not-found)" ] && break
  sleep 2; [ "$i" = 10 ] && fail "compute pod survived suspension"
done
# State intact across the crash+suspend: wake and read back.
mcp get_connection '{"name":"e2ec1"}' | jq -e '.woke_from_suspend == true' >/dev/null || fail "wake after crash"
psql_run e2ec1 "select count(*) from c" | grep -qx 1 || fail "data lost across crash+suspend"

step "drill 2: pageserver restarts under an active compute — queries recover"
mcp create_database '{"name":"e2ec2","cu_limit":2}' | jq -e '.status == "ready"' >/dev/null || fail "e2ec2 not ready"
psql_run e2ec2 "create table c as select g from generate_series(1,10000) g; select count(*) from c" | tail -1 | grep -qx 10000 || fail "load"
kubectl -n $NS rollout restart statefulset/pageserver >/dev/null
kubectl -n $NS rollout status statefulset/pageserver --timeout=180s >/dev/null
ok=""
for i in $(seq 1 30); do
  if [ "$(psql_run e2ec2 "select count(*) from c" 2>/dev/null | tail -1)" = "10000" ]; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || fail "reads never recovered after pageserver restart"
psql_run e2ec2 "insert into c values (0); select count(*) from c" | tail -1 | grep -qx 10001 || fail "writes broken after pageserver restart"

step "drill 3: node reboot — the estate reconverges unattended"
# Before: e2ec1 awake (woken in drill 1), e2ec2 awake. Suspend e2ec1's clock
# is irrelevant — what matters after reboot: actives serve, data intact.
docker restart sspc-control-plane >/dev/null
for i in $(seq 1 60); do kubectl get nodes >/dev/null 2>&1 && break; sleep 3; [ "$i" = 60 ] && fail "apiserver never returned"; done
kubectl wait node --all --for=condition=Ready --timeout=300s >/dev/null
kubectl -n $NS wait --for=condition=Available deploy --all --timeout=300s >/dev/null
for ss in controller-pg safekeeper pageserver; do
  kubectl -n $NS rollout status "statefulset/$ss" --timeout=300s >/dev/null
done
ok=""
for i in $(seq 1 60); do
  if mcp capabilities '{}' 2>/dev/null | jq -e '.platform == "sspc"' >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
[ -n "$ok" ] || fail "MCP never answered after reboot"
ok=""
for i in $(seq 1 60); do
  if [ "$(psql_run e2ec2 "select count(*) from c" 2>/dev/null | tail -1)" = "10001" ]; then ok=1; break; fi
  sleep 3
done
[ -n "$ok" ] || fail "active database never recovered after reboot"
# The platform still does its whole job post-reboot: lifecycle + wake.
mcp get_connection '{"name":"e2ec1"}' >/dev/null || fail "get_connection broken after reboot"
psql_run e2ec1 "select count(*) from c" | grep -qx 1 || fail "e2ec1 data lost across reboot"

step "cleanup"
mcp delete_database '{"name":"e2ec1"}' >/dev/null
mcp delete_database '{"name":"e2ec2"}' >/dev/null

printf '\033[1;32mCHAOS PASS in %ds\033[0m\n' "$(($(date +%s)-T_START))"
