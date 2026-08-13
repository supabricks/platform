#!/usr/bin/env bash
# T7 cold-attach restore (review 002 P0): "the bucket is the database."
# Creates data + a divergent branch, quiesces + flushes to object storage,
# DESTROYS every local-state PVC (pageserver layer cache, safekeeper WAL,
# storage-controller database), rebuilds the cell against the same bucket,
# and proves both timelines serve their exact data and accept writes.
# NOTE: estate-wide by nature — every tenant in the cell is re-attached.
set -euo pipefail
cd "$(dirname "$0")"
NS=sspc-cell
ART=./e2e-artifacts
T_START=$(date +%s)

TOKEN=$(kubectl -n $NS get secret sspc-mcp-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
mcp() {
  curl -sf -m 60 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
    http://localhost:30080/mcp | jq -r '.result.content[0].text'
}
psql_run() {
  for a in 1 2 3 4 5; do
    out=$(kubectl -n $NS exec "$1" -- psql -U cloud_admin -h localhost -p 55433 -d postgres -Atc "$2" 2>&1) && { echo "$out"; return 0; }
    sleep 3
  done
  echo "$out"; return 1
}
step() { printf '\033[1;33m[%3ds] %s\033[0m\n' "$(($(date +%s)-T_START))" "$*"; }
fail() {
  printf '\033[1;31mRESTORE FAIL: %s\033[0m\n' "$*" >&2
  mkdir -p $ART
  kubectl -n $NS logs deploy/sspc-operator --tail 200 > $ART/restore-operator.log 2>&1 || true
  kubectl -n $NS logs pageserver-0 --tail 200 > $ART/restore-pageserver.log 2>&1 || true
  kubectl -n $NS get databases,branches,pods,pvc -o yaml > $ART/restore-resources.yaml 2>&1 || true
  echo "artifacts in $ART" >&2
  exit 1
}
trap 'fail "unexpected error at line $LINENO"' ERR
lsnnum() { local hi lo; IFS=/ read -r hi lo <<< "$1"; echo $(( 16#${hi:-0} * 4294967296 + 16#${lo:-0} )); }

step "pre-clean (idempotent)"
mcp delete_branch '{"name":"e2erstbr"}' >/dev/null 2>&1 || true
mcp delete_database '{"name":"e2erst"}' >/dev/null 2>&1 || true
# Wait for the CRs to actually vanish: finalizer cleanup does real cell-side
# work and an SSA create against a deleting CR fails.
for i in $(seq 1 20); do
  left=$(kubectl -n $NS get database/e2erst branch/e2erstbr --no-headers --ignore-not-found 2>/dev/null)
  [ -z "$left" ] && break
  sleep 2
done

step "create parent (50k rows) + divergent branch (60k rows)"
mcp create_database '{"name":"e2erst","cu_limit":2}' | jq -e '.status == "ready"' >/dev/null || fail "create e2erst"
psql_run e2erst "create table t as select g from generate_series(1,50000) g; select count(*) from t" | tail -1 | grep -qx 50000 || fail "parent load"
mcp create_branch '{"name":"e2erstbr","database":"e2erst","cu_limit":2}' | jq -e '.status == "ready"' >/dev/null || fail "create e2erstbr"
psql_run e2erstbr "insert into t select g from generate_series(1,10000) g; select count(*) from t" | tail -1 | grep -qx 60000 || fail "branch load"

step "quiesce: suspend both (flush LSNs recorded)"
kubectl -n $NS patch database e2erst  --type=merge -p '{"spec":{"suspendAfterSeconds":10}}' >/dev/null
kubectl -n $NS patch branch  e2erstbr --type=merge -p '{"spec":{"suspendAfterSeconds":10}}' >/dev/null
for r in "database/e2erst" "branch/e2erstbr"; do
  for i in $(seq 1 30); do
    [ "$(kubectl -n $NS get "$r" -o jsonpath='{.status.phase}')" = "Suspended" ] && break
    sleep 4; [ "$i" = 30 ] && fail "$r never suspended"
  done
done
TEN=$(kubectl -n $NS get database e2erst -o jsonpath='{.status.tenantId}')
TL_DB=$(kubectl -n $NS get database e2erst -o jsonpath='{.status.timelineId}')
TL_BR=$(kubectl -n $NS get branch e2erstbr -o jsonpath='{.status.timelineId}')
FL_DB=$(kubectl -n $NS get database e2erst -o jsonpath='{.status.flushLsn}')
FL_BR=$(kubectl -n $NS get branch e2erstbr -o jsonpath='{.status.flushLsn}')
[ -n "$TEN" ] && [ -n "$TL_DB" ] && [ -n "$TL_BR" ] || fail "missing status ids"

step "flush to bucket: remote_consistent_lsn >= flush LSN on both timelines"
# No forced checkpoint: /checkpoint is a testing-only pageserver API. The
# cell's tenant_config checkpoint_timeout (chart) uploads the tail within
# seconds — which is the durability posture this drill exists to verify.
for pair in "$TL_DB:$FL_DB" "$TL_BR:$FL_BR"; do
  tl="${pair%%:*}"; fl="${pair##*:}"
  for i in $(seq 1 45); do
    rcl=$(curl -s "http://localhost:30099/v1/tenant/$TEN/timeline/$tl" | jq -r '.remote_consistent_lsn // .remote_consistent_lsn_visible // "0/0"')
    [ "$(lsnnum "$rcl")" -ge "$(lsnnum "${fl:-0/0}")" ] && break
    sleep 2; [ "$i" = 45 ] && fail "timeline $tl never reached the bucket (remote $rcl < flush $fl)"
  done
done

step "DESTROY local state: pageserver layers, safekeeper WAL, controller DB"
kubectl -n $NS scale deploy/sspc-operator --replicas=0 >/dev/null
for ss in pageserver safekeeper controller-pg; do
  kubectl -n $NS scale statefulset/$ss --replicas=0 >/dev/null
done
kubectl -n $NS wait --for=delete pod pageserver-0 pod/safekeeper-0 pod/controller-pg-0 --timeout=120s >/dev/null 2>&1 || true
kubectl -n $NS delete pvc data-pageserver-0 data-safekeeper-0 data-controller-pg-0 --timeout=60s >/dev/null
# The demo MinIO (the bucket) is untouched: it IS the durable database.

step "rebuild the cell against the same bucket"
kubectl -n $NS scale statefulset/controller-pg --replicas=1 >/dev/null
kubectl -n $NS rollout status statefulset/controller-pg --timeout=300s >/dev/null
# The storage controller migrates its schema at STARTUP and caches state —
# it must restart against the fresh database or every pageserver re-attach
# 500s with 'relation "nodes" does not exist' (found by this drill's first
# destroy run).
kubectl -n $NS rollout restart deploy/storage-controller >/dev/null
kubectl -n $NS rollout status deploy/storage-controller --timeout=300s >/dev/null
for ss in safekeeper pageserver; do
  kubectl -n $NS scale statefulset/$ss --replicas=1 >/dev/null
done
for ss in safekeeper pageserver; do
  kubectl -n $NS rollout status statefulset/$ss --timeout=300s >/dev/null
done
kubectl -n $NS scale deploy/sspc-operator --replicas=1 >/dev/null
kubectl -n $NS rollout status deploy/sspc-operator --timeout=120s >/dev/null
# The operator re-registers every tenant with the fresh controller; the
# pageserver re-attaches them from the bucket's remote indexes.
for i in $(seq 1 60); do
  curl -s "http://localhost:30099/debug/v1/tenant" 2>/dev/null | jq -r '.[].tenant_shard_id' 2>/dev/null | grep -q "^$TEN" && break
  sleep 3; [ "$i" = 60 ] && fail "tenant $TEN never re-attached after rebuild"
done

wake() { # retry: the MCP NodePort takes a few seconds to route after the
         # operator scale-up, and the first wake after restore does real work
  for i in 1 2 3 4 5; do
    mcp get_connection "{\"name\":\"$1\"}" 2>/dev/null | jq -e '.connection_uri' >/dev/null 2>&1 && return 0
    sleep 4
  done
  return 1
}

step "the parent serves its exact data from the bucket"
wake e2erst || fail "parent wake failed"
psql_run e2erst "select count(*) from t" | tail -1 | grep -qx 50000 || fail "parent data wrong after restore"

step "the branch serves its divergent data; isolation intact"
wake e2erstbr || fail "branch wake failed"
psql_run e2erstbr "select count(*) from t" | tail -1 | grep -qx 60000 || fail "branch data wrong after restore"

step "restored timelines accept writes"
psql_run e2erst "insert into t values (0); select count(*) from t" | tail -1 | grep -qx 50001 || fail "parent write after restore"
psql_run e2erstbr "select count(*) from t" | tail -1 | grep -qx 60000 || fail "isolation broken after restore"

step "cleanup"
mcp delete_branch '{"name":"e2erstbr"}' >/dev/null
mcp delete_database '{"name":"e2erst"}' >/dev/null

printf '\033[1;32mRESTORE PASS in %ds — the bucket is the database\033[0m\n' "$(($(date +%s)-T_START))"
