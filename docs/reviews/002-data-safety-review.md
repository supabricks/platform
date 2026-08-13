# Review 002: data safety

Status: current M1.5 data-safety contract verified; restore/HA claims still out of scope  
Date: 2026-08-13  
Scope: branch correctness, cleanup/finalizers, credentials, suspend/wake durability, TTL, and recovery claims

## Verdict

The prototype is now conservative for the data-safety promises it currently
makes: a head branch waits for the parent's flushed WAL to be ingested before
allocating its timeline, branch and database deletes refuse unsafe parent
destruction, per-endpoint credentials fail closed, and suspend/wake preserves
data in the exercised path.

Do not expand the claim beyond that. The bucket-portability story remains
unproven until a cold-attach restore test destroys local storage and serves the
same tenant from object storage in a fresh cell. HA also remains deliberately
out of scope: the chart is single-operator, single-safekeeper, and
single-pageserver in the verified configuration.

## Verification performed

Local gates:

```sh
cd platform
cargo fmt -- --check
cargo test --locked
cargo run --locked --bin crdgen | diff -u chart/crds/sspc-crds.yaml -
helm lint chart
cd ui && npm run build
```

Runtime gates:

```sh
cd platform
docker build -t sspc-operator:p1 .
kind load docker-image --name sspc sspc-operator:p1
helm upgrade --install sspc chart -n sspc-cell
kubectl -n sspc-cell rollout restart deploy/sspc-operator
kubectl -n sspc-cell rollout status deploy/sspc-operator --timeout=180s
./e2e/run.sh
./e2e/chaos.sh
```

Observed results:

- `./e2e/run.sh` passed in 181 seconds after the Review 001 fixes and the e2e
  event-polling stabilization.
- `./e2e/chaos.sh` passed in 123 seconds, covering operator crash during
  lifecycle, pageserver restart under active compute, and local kind node
  reboot/reconvergence.

Two issues were found during verification:

- An interim operator image still allowed a retry-state branch miss: the first
  reconcile patched `phase=Provisioning`, then the next reconcile skipped the
  branch-at-head ingestion wait because the guard only checked whether status
  existed. Current code keys the wait on `status.timelineId` instead, so a
  branch is not considered allocated until the timeline actually exists.
- The TTL e2e assertion checked Events once immediately after the branch CR
  disappeared. The `TTLExpired` Event existed, but was not reliably visible at
  that exact instant. The e2e now polls Events through JSON for a bounded
  window.

## Verified invariants

### 1. Storage identity is replay-safe

Tenant and timeline IDs are deterministic functions of Kubernetes UIDs. Replays
converge on the same cell-side resources, and storage-controller create/delete
calls treat already-present or already-absent resources as successful where
safe.

Reference-grade status: acceptable for the current single-writer prototype.

### 2. Head branches do not cut below committed parent WAL

For a branch without `spec.at`, the reconciler reads the parent compute's
`pg_current_wal_flush_lsn()` using that endpoint's Secret, then waits until the
pageserver reports `last_record_lsn >= flush_lsn`. If the parent LSN is
unreadable or ingestion lags past the deadline, the branch stays
`Provisioning` and requeues instead of creating a possibly stale timeline.

The e2e path creates a parent table, branches immediately, writes the branch,
and verifies parent isolation. This failed before the retry-state fix and now
passes.

Reference-grade status: acceptable, with one missing negative test: an induced
ingestion-lag case should prove the branch remains unallocated after timeout.

### 3. Historical branch points are conservative but incomplete

Timestamp branch points resolve through the pageserver. Unusable timestamps set
the branch to `Failed` with a message. Raw LSN branch points are passed through
to the storage controller.

Reference-grade status: not complete. A syntactically valid but bogus LSN still
lands in a reconciler retry loop instead of terminal `Failed`. This is known
debt in `docs/handbook/backlog.md`.

### 4. Destructive operations preserve branch trees

`delete_database` refuses while live child branches exist and names the
children. `delete_branch` refuses while branch-of-branch children exist. The
finalizers repeat those checks as a backstop for direct Kubernetes deletion.

The e2e suite verifies ordered teardown: database delete is refused while
`e2ebr` exists; branch delete is refused while `e2egrand` exists; ordered
deletion then removes the tenant from the storage controller.

Reference-grade status: acceptable for M1.5.

### 5. Cleanup does not depend on best-effort status

Branch cleanup derives the tenant from the owning Database when
`status.tenantId` is missing. The e2e suite deterministically exercises this by
stopping the operator, stripping the branch status tenant, deleting the branch,
restarting the operator, and verifying the storage-controller timeline is gone.

Reference-grade status: acceptable for M1.5.

### 6. Suspend/wake preserves data in the exercised path

The e2e suite creates data, waits for suspend, confirms the pod is gone, wakes
through `get_connection`, and verifies the row still exists. The wake-clock race
from Review 001 is fixed by treating pod start time as a fresh idle baseline.

Reference-grade status: acceptable for the current happy path. Missing coverage:
write traffic racing with suspend and repeated wake/suspend cycles under load.

### 7. Credentials fail closed

Endpoint connection URIs and internal parent-LSN reads require the per-endpoint
Secret. Missing or malformed credentials return structured retriable errors
instead of falling back to a shared password. The e2e suite verifies distinct
database/branch credentials and rejects a wrong password.

Reference-grade status: acceptable for M1.5.

### 8. TTL is cleanup, not durable audit

TTL reaping deletes the CR and finalizers clean cell-side resources. A
`TTLExpired` Kubernetes Event is emitted as operational signal. Events are not a
durable audit log and should not be treated as data-safety evidence.

Reference-grade status: acceptable only because Events are documented as
best-effort. Durable audit remains out of scope.

### 9. Single-node crash/restart recovery reconverges

The chaos suite verifies three local recovery cases: operator restart while an
idle clock is running, pageserver restart under an active compute, and kind node
reboot. In each case, previously written rows remain readable after the system
reconverges.

Reference-grade status: acceptable for the current single-node cell. This does
not prove HA or loss of a quorum member; those remain out of scope.

## Remaining data-safety gaps

### P0: Cold-attach restore is unproven

The design says the bucket is the database, but no test currently destroys local
PVCs/control-plane state and proves a fresh cell can serve the tenant from the
object-store contents alone.

Exit criteria:

- Create data and branches.
- Force a clean object-store flush.
- Destroy the local cell state that is supposed to be cache or rebuildable.
- Start a fresh cell against the same bucket.
- Verify parent and branch data, branch isolation, and continued writes.

Until this exists, do not claim disaster recovery, site portability, or bucket
completeness as verified prototype behavior.

### P1: Bad raw LSNs need terminal classification

Timestamp branch failures are terminal. Raw LSN failures can still surface as
storage-controller errors that the reconciler retries forever.

Exit criteria: classify storage-controller 4xx responses for user-supplied raw
LSNs as branch `Failed`, with an MCP error that tells the caller to fix `at` and
recreate or delete the branch.

### P1: Head-branch lag needs a deterministic negative test

The e2e suite exercises the real immediate-branch race and now passes, but it
does not force pageserver ingestion lag beyond the timeout.

Exit criteria: add a unit/integration test hook or storage-controller fake
proving a branch with `last_record_lsn < parent_flush_lsn` remains without
`timelineId`.

### P1: HA remains unsafe by design

Two operator replicas are not supported, and the safekeeper quorum is one in
the verified chart. That is fine for the M1.5 prototype, but it is not an HA
data-safety story.

Exit criteria: leader election for the operator, tested multi-safekeeper
identity/config, and a chaos test that proves no split-brain or acknowledged
write loss across a failed storage component.

### P2: Client-path coverage still uses in-pod psql

The e2e suite uses `kubectl exec` because the M1 surface is loopback/kind
friendly. It proves server behavior, but not host-side driver behavior through
NodePort.

Exit criteria: gateway or host-side conformance suite with real client drivers,
including reconnect after suspend/wake.

## Decision

The current M1.5 prototype can be treated as reference-grade for local
create/load/branch/isolate/delete/suspend/wake/TTL behavior after Review 001.
It cannot yet be treated as reference-grade for disaster recovery, bucket
portability, or HA. The next data-safety milestone should be the cold-attach
restore job; nothing else will substitute for proving the backup/rebuild story.

## Resolution (2026-08-13)

- **P0 cold-attach restore: EXECUTED.** `e2e/restore.sh` (T7, wired into CI)
  runs the review's exact exit criteria: data + divergent branch created,
  flush to bucket verified (`remote_consistent_lsn >= flush LSN`), then the
  pageserver, safekeeper, and controller-pg PVCs are DESTROYED, the cell
  rebuilt against the same bucket, and both timelines verified — exact row
  counts, isolation, continued writes. **PASS in 95s, unattended.** The drill
  fails closed: flush is verified before anything is destroyed.
  Two real platform findings on the way:
  1. Stock `checkpoint_timeout` (10m) left a quiet timeline's tail WAL out of
     the bucket for up to ten minutes — the durability claim was silently
     time-lagged. The chart now pins the cell tenant default to `10 s`.
  2. The storage controller migrates its schema only at startup and caches
     node state — after a controller-pg rebuild it 500s every re-attach
     (`relation "nodes" does not exist`) until restarted. Encoded in the
     drill and the runbook.
  Also answered: a fresh (empty-WAL) safekeeper is re-seeded by the
  walproposer on first wake — no manual safekeeper recovery needed at this
  scale.
- **P1 bad raw LSNs: EXECUTED.** Storage-controller 4xx on a user-supplied
  branch point is terminal `Failed` with the reason (5xx/network stays
  retriable). Backlog debt #4 struck.
- **P1 head-branch lag negative test: EXECUTED.** The wait decision is a pure
  function (`head_wait_verdict`) with unit tests proving sustained lag never
  yields `Ready` — past the deadline the branch is held, not cut. A third
  test pins the retry-state regression (Provisioning-without-timelineId is
  unallocated).
- **P1 HA / P2 client-path**: remain deliberately deferred per this review's
  own acceptance (M2 line; backlog items unchanged).
