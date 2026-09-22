# SY06: shared sync controls and governed snapshot service authority

[Sync plan](../plans/analytical-sync-implementation.md) · [Operating guide](../handbook/managed-sync.md)

Status: source implementation for review. This is the first SY06 slice, not the
full SY06 exit. Local console/CLI/MCP support all three modes. Governed ingress
supports managed **snapshot/full** policies with service authority. Governed
triggered/continuous remain explicitly unavailable: UC storage grants currently
expose an unversioned Delta location, which is insufficient to authorize one
epoch of a mutable incremental root. No capability flag claims that path works.

## Shared control contract

`sync::Command` is the common local API and browser contract. Stdio MCP exposes
`managed_snapshots`, with the checked [command schema](../../schemas/sync-command-v1.schema.json).
MCP wraps the response in `value` so both object and list results obey its object
output contract. Browser controls use the API dispatcher, including native-cell
and matching-worker checks; they cannot bypass runtime preflight.

`inspect` reads source identity, availability and prerequisites. It creates no
policy, schedule, slot or worker. `source_qualification=not_probed` deliberately
does not claim a live SQL schema inspection. Source workers qualify the complete
supported group before any publication. Opening a page only lists existing state.

The console's Analytics page offers settings, explicit creation, run, safe pause,
resume, cancel, deletion and recent run history. Continuous admission discloses
compute wakefulness and bounded resource use. Configuration updates preserve
existing export limits. Requests carry policy revisions and stable retry keys;
a lost response offers an explicit identical retry, never an automatic write.

The surface shows the policy revision, last success, next due time, pinned epoch,
source observation, captured/published boundaries, backlog/spool/WAL and observed
oldest-unpublished-commit age. Missing, stale or disconnected lag stays unknown.
Snapshot age is not used as a lag estimate. Existing SQL/notebook readers keep
their selected epoch; a newer publication never changes their inputs.

`sync_controls=1` negotiates this surface independently of
`managed_snapshot_scheduling`, `incremental_triggered`, `continuous_sync` and
`sync_event_triggers`. The local console checks native/runtime availability for
incremental flags. Governed flags advertise only snapshot scheduling. An older
runtime without `sync_controls` renders an unavailable state and keeps existing
snapshot functions usable. Event triggers and reverse sync remain unavailable.

## Reviewed resync

`review_resync` produces an immutable review hash over policy revision, source
timeline and capture identity, together with the full-copy consequence. `resync`
requires that hash, the expected revision and an idempotency key. In one command
transaction it cancels unfinished work, pauses the policy, retires the owned
capture and clears observation intent. It preserves published epochs and reader
references. Resume is refused until old capture cleanup has completed. Explicit
resume then enrolls a new generation and performs the isolated bootstrap.

This is intentionally a two-step operation: approval does not silently create a
new source feed while slot cleanup is pending. Triggered/continuous resync remains
local only until governed incremental storage is qualified.

## Governed snapshot authority

The signed-in `workspace.sync` command carries a deployment, the same typed sync
command, expected authorization-policy revision and (on create) service principal.
The existing authenticated CLI/MCP project-control ingress can use this envelope.
Both authorization and sync receipt journals commit together, with internal keys
scoped by actor and deployment. Permissions are checked before replaying receipts.

| Operation | Required authority |
| --- | --- |
| Inspect/list/status/history | Project membership and branch `read_sync` |
| Create/change/run/pause/resume/cancel/delete | Project membership, branch `read` and `manage_sync` |
| Internal export and epoch publication | Enabled service principal, project membership, branch `read` and `execute_sync` |
| Inspect/select a managed result for sharing | Branch `read_sync` and still-live producer authority |
| Publish/share into UC | Existing project-administrator and branch `share` checks, plus result access |
| Query shared tables | Existing UC grants and governed isolated execution |

No deployment role implicitly grants these branch capabilities. Choosing a
service does not grant its authority to the caller: the saved policy fixes the
deployment, branch, source lineage and service identity. No browser token or
bearer credential is persisted in the policy. Logout/session expiry of the
manager does not terminate the background service. Existing whole-branch source
profile checks also run on the private frozen export before reading its rows;
RLS and other unsupported profiles fail closed.

Schema 28 adds a service sync-generation fence and the three data capabilities.
The policy records the admitted service generation and authorization revision.
Any authorization revision change in the producer deployment conservatively
fences its service policies, even an unrelated grant change. Service revoke,
disable/enable and session rotation also invalidate existing service authority.
Review grants and explicitly delete/recreate the policy to establish fresh
authority; no background renewal recovers access automatically.

Export ticks, publication commits and new snapshot reads recheck authority. The
durable export request key covers the crash window before its run link commits.
Revocation also dirties the UC authorization cache and increments its revision,
fencing existing isolated executions. Catalog grant planning refuses a published
managed snapshot with invalid producer authority; retire that publication before
reconciling grants. This conservative cache invalidation can affect availability
across the local catalog; it never restores access to revoked files.

Audit records include admission/configuration, run transitions and committed
epoch/source boundaries, grant changes and revocation. Records contain identifiers,
not row contents, bearer tokens or storage credentials. Existing audit-capacity
and restored-state admission gates also apply to service workers.

## Qualification and remaining SY06 work

Recorded evidence: [Linux native, 7 checks](sy06-evidence/linux-native.json) and
[Chromium/native console, 16 checks](sy06-evidence/linux-browser.json). Rust
validation passed 332 core/local tests across the suite and targeted recovery/state
reruns (3 intentional ignores), with a final 35-test sync rerun and 8 release
evidence tests. These are source checks, not installed-release qualification.

Source qualification includes command/retry/revision tests, schema-27 migration
and stopped recovery, real native background export after manager revocation,
service revocation, denied/cross-project access, RLS refusal and pinned Sail
readers. The browser journey covers no implicit enrollment, continuous disclosure,
lost create response, live progress, reviewed resync, cleanup and old-runtime
fallback. Evidence and exact commands are recorded in the PR and operating guide.

Full SY06 remains open for:

- Version-aware governed storage grants and isolated readers for incremental
  roots, including positive UC dataset-binding and notebook journeys on v2.
- Governed triggered/continuous capture identities and worker qualification;
  the present service implementation is qualified for frozen snapshot workers.
- Optional constrained new-reader admission. Proposed input: full source/capture
  identity plus minimum published LSN and/or maximum observed lag, with a bounded
  deadline. Admission must resolve one immutable epoch satisfying the request.
  Unknown/stale progress returns `freshness_unavailable`; a deadline must never
  weaken the constraint or retarget a previously pinned session. No API currently
  promises this wait operation.
- Final Linux/macOS integration evidence and SY08's exact installed-release gate.

SY07 resource maintenance and SY08 release qualification remain separate. These
source changes do not imply a newly qualified installed archive.
