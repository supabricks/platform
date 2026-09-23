# SY06: shared sync controls and governed service authority

[Sync plan](../plans/analytical-sync-implementation.md) · [Operating guide](../handbook/managed-sync.md)

Status: source implementation complete. Local console/CLI/MCP and governed
ingress support snapshot, triggered and continuous policies. Governed policies
bind a scoped service authority; shared incremental results use immutable
per-epoch catalog views. [Platform #84](https://github.com/supabricks/platform/pull/84)
tracks the Linux/macOS merge gates; [console #14](https://github.com/supabricks/console/pull/14)
is merged. SY07 maintenance and SY08 exact installed-release qualification remain separate.

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
incremental flags. Governed flags require the same matching native runtime. An older
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
new source feed while slot cleanup is pending. The governed flow uses the same
review, cleanup and explicit-resume contract.

## Governed service authority

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
manager does not terminate the background service. The whole-branch source profile is checked before each governed capture worker
starts, and again on the private frozen export before reading its rows;
RLS and other unsupported profiles fail closed. The only capture-specific routine
exception is the engine-owned DDL fence: its schema ownership and complete durable
identity, exact function OID and shape, and both enabled owned event triggers must
match. A matching name alone grants no exception. Permission reconciliation skips
no-op ownership DDL; capture ignores GRANT/REVOKE because these do not change row
encoding. Structural DDL remains fenced. Capture identities include the
saved service generation and authorization revision.

Schema 28 adds a service sync-generation fence and the three data capabilities.
The policy records the admitted service generation and authorization revision.
Any authorization revision change in the producer deployment conservatively
fences its service policies, even an unrelated grant change. Service revoke,
disable/enable and session rotation also invalidate existing service authority.
Review grants and explicitly delete/recreate the policy to establish fresh
authority; no background renewal recovers access automatically.

Export ticks, publication commits and new snapshot reads recheck authority. The
durable export request key covers the crash window before its run link commits.
Bootstrap and incremental artifacts are also resolved through their durable capture
identity, including the window before the parent sync run records its result.
Revocation also dirties the UC authorization cache and increments its revision,
fencing existing isolated executions. Catalog grant planning refuses a published
managed snapshot with invalid producer authority; retire that publication before
reconciling grants. This conservative cache invalidation can affect availability
across the local catalog; it never restores access to revoked files.

Audit records include admission/configuration, run transitions and committed
epoch/source boundaries, grant changes and revocation. Records contain identifiers,
not row contents, bearer tokens or storage credentials. Existing audit-capacity
and restored-state admission gates also apply to service workers.

## Immutable catalog views of incremental epochs

UC grants identify an unversioned storage location. Granting the internal mutable
Delta root would expose later commits and historical files. An explicit sharing
review instead plans a frozen version-zero view of the selected table versions.
Planning verifies the recorded log prefix and selects only active parquet files.
It excludes removed, future and orphan files and discards commit metadata and
row statistics. Unsupported Delta actions, features, paths or mismatched hashes
fail closed.

Review is read-only and reports `retention.copies=true` and `view_bytes`. After
admission durably pins the epoch, publication copies and verifies selected files,
writes minimal Delta logs, fsyncs and atomically renames the complete view under
`analytics/generations/<artifact>/shared`. Original epoch/hash identity remains
in the catalog publication; the internal view descriptor binds that source hash.
SQL, bound datasets, notebooks and isolated workloads read the frozen view.
Later producer commits cannot change its contents or retarget existing readers.

This is a bounded copy when explicitly sharing an epoch, not on each continuous
batch: at most 256 MiB, 4096 files and 128 tables per view; input logs are bounded
to 2 MiB each and 32 MiB in total. Catalog capacity conservatively counts twice
the retained incremental descriptor inventory for original plus view storage.
The original generation and its view remain retained until publication, binding
and reader references drain. Stopped catalog recovery validates and relocates
both identities. Long-term compaction and crash-residue maintenance belong to SY07.

## Optional constrained reader admission: design contract

A future request may supply the complete source/capture identity, minimum
published LSN and/or maximum observed lag, plus a bounded wait deadline. Admission
must resolve one immutable epoch satisfying every supplied constraint against
that identity and a current observation. Unknown, disconnected or stale progress
returns `freshness_unavailable`. Expiry must never weaken constraints, substitute
another source generation or retarget an existing reader. No current API promises
this wait operation; observed freshness is available in the shipped controls.

## Qualification

Evidence: [native UC/Sail, 14 checks](sy06-evidence/linux-incremental-native.json),
[real UC/gVisor isolation](sy06-evidence/linux-incremental-isolation.json),
[signed-in browser, 9 checks](sy06-evidence/linux-governed-browser.json), and the
[earlier local browser journey, 17 checks](sy06-evidence/linux-browser.json).
The full core/local Rust suite passed 335 tests (four external-runtime ignores),
with a final 196-test local-library rerun and 12 capture unit tests passing.
The [continuous regression](sy06-evidence/linux-continuous-regression.json) passed
all eight checks with the unchanged 5000 ms target: sustained p95 4580 ms and the
200-row burst 2970 ms. This is a measured source workload, not a production SLA.
See [operating commands](../handbook/managed-sync.md).
These are source checks, not installed-release qualification.

The isolation test grants two users different tables and proves that the selected
epoch excludes old/deleted/future data, admits read-only files, denies host/network
access and fences existing workloads after authorization/session revocation.
The native suite covers manager logout, service revocation, RLS refusal, pinned
Sail readers, cross-project bindings, notebook restart and relocated catalog views.
The signed-in browser covers continuous disclosure, explicit service grants,
incremental result sharing, reviewed resync, cleanup, resume and service revocation.

Linux/macOS CI is required before merge. The native catalog fixture combines the
verified baseline notebook dependencies with current analytical workers. Using
the baseline reader directly omits dataset bindings and capture support; this
fixture is not an unchanged installed-release qualification.

[Native CI on both platforms](https://github.com/supabricks/platform/actions/runs/35819986230)
passed at `d153fca`, before the notebook fixture correction. The
[Linux sample](sy06-evidence/linux-ci-continuous.json) measured sustained p95
3431 ms and a 3107 ms burst; the [macOS sample](sy06-evidence/macos-ci-continuous.json)
measured sustained p95 4156 ms and a 3157 ms burst. The earlier SY06 macOS native run
failed the unchanged 5000 ms continuous burst target (6219 ms); its sustained p95
was 3947 ms. That failed timing sample is not passing evidence. SY07 resource
maintenance and SY08 exact archive qualification remain separate delivery slices.
