#!/usr/bin/env bash
# T4 smoke e2e (RFC 012 §4) + T3 idempotency. Drives the platform exclusively
# through MCP (the operator's own identity — P2's RBAC lesson), kubectl for
# read-only assertions. Assumes the platform is up (install/up.sh).
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
psql_run() { # in-pod psql (loopback-bound host ports are unreachable from containers); retry x3
  for a in 1 2 3 4 5; do
    out=$(kubectl -n $NS exec "$1" -- psql -U cloud_admin -h localhost -p 55433 -d postgres -Atc "$2" 2>&1) && { echo "$out"; return 0; }
    sleep 3
  done
  echo "$out"; return 1
}
step() { printf '\033[1;36m[%3ds] %s\033[0m\n' "$(($(date +%s)-T_START))" "$*"; }
fail() {
  printf '\033[1;31mFAIL: %s\033[0m\n' "$*" >&2
  mkdir -p $ART
  kubectl -n $NS logs deploy/sspc-operator --tail 200 > $ART/operator.log 2>&1 || true
  kubectl -n $NS get databases,branches,pods,svc -o yaml > $ART/resources.yaml 2>&1 || true
  echo "artifacts in $ART" >&2
  exit 1
}
trap 'fail "unexpected error at line $LINENO"' ERR

step "pre-clean (idempotent)"
for r in e2egrand e2epit e2epts e2ebr e2ettl e2enostat; do mcp delete_branch "{\"name\":\"$r\"}" >/dev/null 2>&1 || true; done
for r in e2edb e2esleep e2echurn; do mcp delete_database "{\"name\":\"$r\"}" >/dev/null 2>&1 || true; done
mcp unenroll_database '{"name":"e2enrolled"}' >/dev/null 2>&1 || true
sleep 3

step "capabilities"
mcp capabilities '{}' | jq -e '.features.scale_to_zero == true' >/dev/null
mcp get_cu_ledger '{}' | jq -e '.physical_cu > 0 and .promised_cu >= .active_cu' >/dev/null || fail "cu ledger"

step "T3 idempotency: create e2edb twice -> exactly one CR"
mcp create_database '{"name":"e2edb"}' >/dev/null
URI=$(mcp create_database '{"name":"e2edb"}' | jq -r .connection_uri)
[ "$URI" != "null" ] || fail "no connection_uri from create"
[ "$(kubectl -n $NS get databases --no-headers | grep -c '^e2edb ')" = "1" ] || fail "duplicate CRs"

step "load 100k rows"
psql_run e2edb "create table t as select g from generate_series(1,100000) g; select count(*) from t" | tail -1 | grep -qx 100000 || fail "load"

step "branch + isolation"
BURI=$(mcp create_branch '{"name":"e2ebr","database":"e2edb"}' | jq -r .connection_uri)
psql_run e2ebr "insert into t select g from generate_series(1,50000) g; select count(*) from t" | tail -1 | grep -qx 150000 || fail "branch write"
psql_run e2edb "select count(*) from t" | grep -qx 100000 || fail "parent isolation"

step "H3 per-endpoint credentials: distinct, real, enforced"
P_DB=$(echo "$URI"  | sed -E 's|.*cloud_admin:([^@]+)@.*|\1|')
P_BR=$(echo "$BURI" | sed -E 's|.*cloud_admin:([^@]+)@.*|\1|')
[ -n "$P_DB" ] && [ "$P_DB" != "sspc-p0" ] || fail "e2edb still on the shared static password"
[ "$P_DB" != "$P_BR" ] || fail "database and branch share a credential"
# The credential is enforced on the wire (cross-pod through the Service):
R=$(kubectl -n $NS exec e2ebr -- env PGPASSWORD="$P_DB" psql -h e2edb.$NS.svc.cluster.local -p 55433 -U cloud_admin -d postgres -Atc "select 1" 2>&1) && [ "$R" = "1" ] || fail "right password refused: $R"
if kubectl -n $NS exec e2ebr -- env PGPASSWORD=not-the-password psql -h e2edb.$NS.svc.cluster.local -p 55433 -U cloud_admin -d postgres -Atc "select 1" >/dev/null 2>&1; then
  fail "wrong password accepted"
fi

step "H2 branch-at-time: e2epit (LSN) lacks rows written after the mark"
sleep 2
MARK=$(psql_run e2edb "select pg_current_wal_flush_lsn()::text" | tail -1)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sleep 2
psql_run e2edb "insert into t select g from generate_series(1,23456) g" >/dev/null
psql_run e2edb "select count(*) from t" | grep -qx 123456 || fail "parent post-mark load"
mcp create_branch "{\"name\":\"e2epit\",\"database\":\"e2edb\",\"at\":\"$MARK\",\"cu_limit\":2}" | jq -e '.status == "ready"' >/dev/null || fail "pit branch not ready"
psql_run e2epit "select count(*) from t" | grep -qx 100000 || fail "pit branch sees post-mark rows"
mcp delete_branch '{"name":"e2epit"}' >/dev/null

step "H2 branch-at-time: e2epts (timestamp) resolves via the pageserver"
mcp create_branch "{\"name\":\"e2epts\",\"database\":\"e2edb\",\"at\":\"$TS\",\"cu_limit\":2}" | jq -e '.status == "ready"' >/dev/null || fail "timestamp branch not ready"
psql_run e2epts "select count(*) from t" | grep -qx 100000 || fail "timestamp branch sees post-mark rows"
mcp delete_branch '{"name":"e2epts"}' >/dev/null

step "H2 branch-of-branch: e2egrand forks e2ebr, not the database"
mcp create_branch '{"name":"e2egrand","database":"e2edb","parent":"e2ebr","cu_limit":2}' | jq -e '.status == "ready"' >/dev/null || fail "grand branch not ready"
psql_run e2egrand "select count(*) from t" | grep -qx 150000 || fail "grand branch should carry e2ebr's 150k rows"

step "cleanup without status: timeline reclaimed anyway (review 001 P0-2)"
mcp create_branch '{"name":"e2enostat","database":"e2edb","cu_limit":2}' | jq -e '.status == "ready"' >/dev/null || fail "nostat branch not ready"
NTL=$(kubectl -n $NS get branch e2enostat -o jsonpath='{.status.timelineId}')
NTEN=$(kubectl -n $NS get branch e2enostat -o jsonpath='{.status.tenantId}')
[ -n "$NTL" ] && [ -n "$NTEN" ] || fail "nostat branch has no status ids"
# Deterministically exercise the status-missing cleanup path: with the
# operator stopped, strip the tenant from status and delete — no reconcile
# can re-write it before the finalizer runs.
kubectl -n $NS scale deploy/sspc-operator --replicas=0 >/dev/null
kubectl -n $NS wait --for=delete pod -l app=sspc-operator --timeout=60s >/dev/null 2>&1 || true
kubectl -n $NS patch branch e2enostat --subresource=status --type=merge -p '{"status":{"tenantId":null}}' >/dev/null
kubectl -n $NS delete branch e2enostat --wait=false >/dev/null
kubectl -n $NS scale deploy/sspc-operator --replicas=1 >/dev/null
kubectl -n $NS rollout status deploy/sspc-operator --timeout=120s >/dev/null
for i in $(seq 1 20); do
  gone_cr=$(kubectl -n $NS get branch e2enostat --no-headers --ignore-not-found)
  gone_tl=$(curl -s "http://localhost:30099/v1/tenant/$NTEN/timeline" | jq -r --arg t "$NTL" 'any(.[]; .timeline_id == $t)')
  [ -z "$gone_cr" ] && [ "$gone_tl" != "true" ] && break
  sleep 2; [ "$i" = 20 ] && fail "status-less delete leaked timeline $NTL (cr='$gone_cr' present=$gone_tl)"
done

step "idle detection: short-lived clients keep it awake (review 001 P1-1)"
mcp create_database '{"name":"e2echurn","suspend_after_seconds":20,"cu_limit":2}' >/dev/null
for i in $(seq 1 6); do psql_run e2echurn "select 1" >/dev/null; sleep 7; done
[ "$(kubectl -n $NS get database e2echurn -o jsonpath='{.status.phase}')" = "Active" ] || fail "suspended despite session churn"
mcp delete_database '{"name":"e2echurn"}' >/dev/null

step "scale-to-zero: e2esleep (suspendAfter=20s)"
mcp create_database '{"name":"e2esleep","suspend_after_seconds":20}' >/dev/null
SURI=$(mcp get_connection '{"name":"e2esleep"}' | jq -r .connection_uri)
psql_run e2esleep "create table z as select 1 as v; select count(*) from z" >/dev/null
for i in $(seq 1 30); do
  [ "$(kubectl -n $NS get database e2esleep -o jsonpath='{.status.phase}')" = "Suspended" ] && break
  sleep 4; [ "$i" = 30 ] && fail "never suspended"
done
for i in $(seq 1 8); do
  POD_LEFT=$(kubectl -n $NS get pod e2esleep --no-headers --ignore-not-found)
  [ -z "$POD_LEFT" ] && break; sleep 2
done
[ -z "$POD_LEFT" ] || fail "pod survived suspension"

step "wake via get_connection (budget 10s)"
W=$(mcp get_connection '{"name":"e2esleep"}')
echo "$W" | jq -e '.woke_from_suspend == true' >/dev/null || fail "did not report wake"
WS=$(echo "$W" | jq -r .wake_seconds)
awk "BEGIN{exit !($WS <= 10)}" || fail "wake took ${WS}s (>10s)"
psql_run e2esleep "select count(*) from z" | grep -qx 1 || fail "data lost across suspend"

step "TTL: e2ettl branch (45s) reaps itself"
mcp create_branch '{"name":"e2ettl","database":"e2edb","ttl_seconds":45}' >/dev/null
for i in $(seq 1 30); do
  [ -z "$(kubectl -n $NS get branch e2ettl --no-headers --ignore-not-found)" ] && break
  sleep 5; [ "$i" = 30 ] && fail "TTL never reaped"
done
for i in $(seq 1 10); do
  kubectl -n $NS get events -o json \
    | jq -e '.items[] | select(.reason == "TTLExpired" and .involvedObject.name == "e2ettl")' >/dev/null \
    && break
  sleep 1; [ "$i" = 10 ] && fail "no TTLExpired event"
done

step "enrollment: attach existing PG, zero migration"
E=$(mcp enroll_database '{"name":"e2enrolled","connection_uri":"postgresql://postgres:postgres@controller-pg.sspc-cell.svc.cluster.local:5432/storage_controller"}')
echo "$E" | jq -e '.phase == "Reachable"' >/dev/null || fail "enrolled not Reachable: $E"
mcp list_databases '{}' | jq -e '[.[] | select(.kind == "enrolled" and .name == "e2enrolled")] | length == 1' >/dev/null || fail "enrolled missing from estate list"
mcp unenroll_database '{"name":"e2enrolled"}' >/dev/null

step "H1 safe deletes: refusal names children, then ordered teardown"
mcp delete_database '{"name":"e2edb"}' | jq -e '.reason | test("e2ebr")' >/dev/null || fail "db delete guard missing/unnamed"
kubectl -n $NS get database e2edb >/dev/null 2>&1 || fail "guard deleted the database anyway"
mcp delete_branch '{"name":"e2ebr"}' | jq -e '.reason | test("e2egrand")' >/dev/null || fail "branch delete guard missing/unnamed"

step "cleanup + cell-side verification"
TEN=$(kubectl -n $NS get database e2edb -o jsonpath='{.status.tenantId}')
mcp delete_branch '{"name":"e2egrand"}' >/dev/null
mcp delete_branch '{"name":"e2ebr"}' >/dev/null
mcp delete_database '{"name":"e2edb"}' >/dev/null
mcp delete_database '{"name":"e2esleep"}' >/dev/null
sleep 5
curl -s http://localhost:30099/debug/v1/tenant | jq -r '.[].tenant_shard_id' | grep -q "^$TEN" && fail "tenant survived delete"

printf '\033[1;32mE2E PASS in %ds\033[0m\n' "$(($(date +%s)-T_START))"
