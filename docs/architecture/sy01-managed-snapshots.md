# SY01 — Managed snapshot policies and scheduling

[Sync plan](../plans/analytical-sync-implementation.md) · [Delivery status](../plans/status.md)

Status: implementation under review, 2026-09-22. Catalog schema **23**.
This slice adds local-owner managed **snapshot/full** refreshes through A01–A03.
It does not implement triggered incremental or continuous synchronization, and
it does not change the qualified alpha.35 archives or running installations.

## User contract

Create a policy for one explicit local deployment, project, branch and `postgres`
database. Each run discovers and validates the complete A01 application-table set
at its frozen snapshot boundary. Subsets, other databases and automatic incremental
enrollment are unavailable. Names resolve to immutable branch IDs when enrolling;
renaming/selecting another branch never retargets the policy.

```sh
supabricks sync create --branch main --every-seconds 300 --key analytics-policy
supabricks sync list
supabricks sync show POLICY_ID
supabricks sync run POLICY_ID --revision 1 --key first-refresh
supabricks sync status RUN_ID
supabricks sync runs POLICY_ID --limit 50
supabricks sync pause POLICY_ID --revision 1 --key pause-policy
supabricks sync resume POLICY_ID --revision 2 --key resume-policy
supabricks sync cancel RUN_ID --key cancel-refresh
```

All commands accept the existing `--project` and `--data-dir` selectors. Creation
without `--every-seconds` creates a manual-only policy; it does not immediately
export. `sync update POLICY_ID --revision N` replaces the complete configuration;
omitting `--every-seconds` removes the schedule. `sync delete` requires a revision,
fences pending work and retains history and previously published epochs.

Create/update accept `--max-bytes` and `--timeout-ms` using existing A01 limits
(default 1 GiB and 300 seconds). The native export remains isolated from primary
bulk reads. Refresh may wake source compute. There is **no logical capture,
replication slot, change spool or extra WAL retention between SY01 runs**.

## Schedule and overlap decisions

Only fixed elapsed intervals, 60 seconds to 30 days, in **UTC**, with
`missed_run=coalesce` are supported. These are not local calendar/cron schedules;
IANA zones, daylight-saving adjustments and events are rejected. The first due time
is admission time plus the interval. Subsequent due times preserve that phase.
After downtime or a forward clock step, admit at most one catch-up and advance
past all missed intervals. A backward clock step waits until the stored due time.

One policy owns each source group. One queued/starting/running run per group and
one actual A01 export per installation are allowed. Scheduled ticks that overlap
an active run coalesce into that run and advance next-due; they do not accumulate
another run. Manual overlap with a different key returns conflict. Queue order is
FIFO across groups. A stopped daemon cannot execute schedules; restart resumes them
without a browser, source checkout or open client connection.

Pause, configuration update and delete cancel all unfinished runs before revising
the policy. Already published success is reconciled first and cannot be cancelled.
Resume starts a new interval from resume time, without replaying paused intervals.

## Durable state and publication fence

`sync_policies` records immutable installation/deployment/project/tenant/timeline/
branch identity, database/group scope, explicit local-owner authority, configuration,
revision, state, due time and last successful epoch. `sync_runs` records trigger,
policy revision and immutable run configuration, admission/scheduled/finish times, A01 refresh ID, published epoch,
source LSN and bounded diagnostic codes. `sync_requests` records immutable command
receipts. Policies have active/paused/blocked/deleted states; runs have
queued/starting/running/succeeded/failed/cancelled states.

All mutations need retry keys; changing parameters under an existing key conflicts.
Receipts return the original accepted result, including after a lost response or
restart. Poll show/status for current state. Expected policy revisions prevent stale
writes. Run admission and scheduled due-time advancement use SQLite savepoints;
queue/writer uniqueness is enforced in SQLite as well as admission code.

The daemon records starting intent before admitting an export with an internal key
derived from run ID. After a crash between export commit and run linkage, reconciliation
finds that exact operation and atomically repairs the link and analytical-refresh
marker. It never submits a second export for that run. A01 retains responsibility
for worker restart outcomes and hidden-branch cleanup; interrupted exports may fail
and require a new run. They are not reported as successful or silently retried.

Cancellation atomically records the run outcome, prevents automatic publication,
marks the native export for cancellation and fences any staged publication. Both
A02 publication admission and its final atomic epoch commit recheck the current
policy/run revision, source lineage and authority, including the unlinked crash
window. Cleanup of completed discarded files is reconciled through A02 GC.
The previous complete epoch remains available. Pinned SQL/notebook sessions and UC
publications retain their existing versions; refresh does not retarget readers or
publish a new shared catalog revision.

## Security, recovery and bounds

Commands are available on the private local application API as
`{"action":"managed_snapshots","command":{...}}`, on the CLI, and through the
local console analytical command adapter as
`{"action":"managed_snapshots","command":{...}}`. The console's product controls
and MCP-specific tools remain SY06 work. Each adapter uses the same journal contract.
Capability discovery separately reports managed snapshot scheduling as supported,
with incremental triggered, continuous and event triggers false.

The daemon revalidates source identity and liveness before execution and publication.
Deleted/expired, changed-lineage, or governed-enrolled sources block the whole policy
and cancel unfinished work. Governed sources cannot enroll through SY01. The signed-in
governed command model does not expose this capability. Background execution currently
uses installation-owner authority, never an expiring browser token. SY06 must introduce
and qualify explicit source/policy/capture/epoch service scopes, revocation fencing
and audit before governed scheduling is enabled.

Stopped backup/restore carries declarative policy/history, but restores policies
paused with a new revision and an explicit resume requirement. Upgrade from catalog
22 uses the existing verified stopped-backup migration path; ordinary startup cannot
silently migrate. Old backups remain restorable with their matching release. Source
packages do not serialize runtime policies or runs.

Admission bounds are 128 policy records (including tombstones), 32 active runs,
10,000 run-history records and 20,000 request receipts per installation. Exhaustion
fails visibly rather than dropping retry receipts or history. History list responses
are limited to 100 records. Run output/deadline/free-disk bounds are inherited from
A01. Snapshot retention remains explicit A02 GC with existing reader protection;
SY01 adds no automatic snapshot deletion, spool retention or incremental compaction.

The reported source LSN is the actual A01 cut, available after publication, rather
than a fabricated run-admission boundary. Unknown replication lag stays unknown;
last-success/publication age is not commit-to-visibility lag. Fixed incremental
catch-up boundaries belong to SY04.

## Qualification

`cargo test -p supabricks-local` covers durable retries, changed request rejection,
revision conflicts, overlapping runs, exact/missed/backward-time schedules,
pause/resume/delete, project isolation, lineage/governed transitions, crash repair,
staged-publication cancellation and success reconciliation. The recovery suite
includes catalog 22 → 23 migration and interrupted upgrade boundaries.

`e2e/native/snapshot_policies.py` uses the real daemon, bundled PG17/Neon/storage,
Delta writer/reader and Sail. It covers CLI/API retries, two-table publication,
a pinned Sail reader during refresh, native cancellation/cleanup, multiple missed
intervals simulated in a stopped fixture, and paused-policy restart. It runs in the
Linux x86_64/macOS arm64 native-cell workflow. A fresh installed-release qualification
with catalog 23 is still required; source/native tests do not qualify an archive.

Local Linux validation on 2026-09-22 passed the complete local-crate suite,
all 23 recovery tests, 12 focused policy/fault tests, the existing analytical
publication regressions, 8 release-evidence collector tests and all 6 native
managed-snapshot scenarios. The native fixture used the qualified alpha.35
PG17/Neon/storage bundle with the newly built platform binary and the locked
source analytical environment. The first fixture setup selected the standalone
Python runtime instead of its matching analytical environment; rerunning with
the locked source environment passed. This is source/native validation, not a
new installed archive qualification. Both native targets run the retained harness
in CI; CI status belongs to the PR checks.
