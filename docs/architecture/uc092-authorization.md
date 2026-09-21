# UC09.2: project policy and execution admission

UC09.2 adds an authenticated project control API and a durable authorization
boundary. The existing local-owner workflow remains an operator interface.
Shared ingress and workload launch remain disabled until the later governance
slices qualify data authority and execution isolation.

UC09.3 extends this boundary with [UC-checked catalog discovery and grants](uc093-catalog-grants.md). The inventory below records the UC09.2 slice.

## Authority and roles

Policy is scoped to the existing deployment UUID, not an arbitrary checkout path,
runtime project name, or client-selected actor. A fresh UC09.1 credential supplies
the realm, actor and channel. The daemon verifies that context, reads the current
policy, and commits the checked mutation and audit record together. CLI, MCP and
the browser adapter use this same handler.

| Assignment | Rights |
| --- | --- |
| Viewer | Discover this project; read its governed source revisions and policy; inspect own admission records |
| Editor | Viewer rights plus save a new immutable source revision |
| Administrator | Editor rights plus manage project role assignments |
| Explicit `execute` grant | Create an admission intent for an immutable source revision; still requires project membership |
| Explicit `act_as` grant | Use a named service principal for one exact approved source revision; both actor and service also need execution grants and project membership |
| Explicit `stop_any` grant | Inspect/cancel another actor's admission within this project |

Role assignments and grants may name stable principals or platform-managed
groups. Roles never imply execution, service delegation, catalog or PostgreSQL
rights. A project administrator cannot grant execution or service-principal use;
the trusted operator manages those explicit grants. The bootstrap realm identity
remains reserved for later complete administration UX; it does not bypass these
checks through the project API.

Service credentials need the explicit `project:control` scope in addition to
`identity:self`, and still need project assignments. New interactive logins
receive the control scope, which itself grants no projects. Existing UC09.1
sessions retain their original scope until the user signs in again.

## Versioned API and coverage inventory

The `authorized` control-socket request wraps a version-1 credential and a closed
project command. Actor/realm/effective context cannot be supplied by clients.
`effective_principal` on an admission is a requested service identity, subject to
all delegation checks; it is not an authenticated assertion.

| Surface | Enforcement |
| --- | --- |
| `identity control --session-file FILE --request-file FILE` | Sends the typed command through authenticated daemon admission |
| Authenticated MCP `project_control` | Sends the same command; credentials are process configuration, not tool arguments |
| `POST /auth/v1/control` in the browser preview | Exact loopback Host, exact Origin, application/json, bounded body, browser cookie and `X-CSRF-Token`; then the same daemon handler |
| `projects` / `project` | Filters by current role before returning names or details; no host paths or credentials |
| `policy` / `set_role` | Project membership for reads; administrator role plus expected policy revision for role mutation |
| `sources` / `source` / `save_source` | Project role, deployment-bound immutable revision, expected source head and policy revision |
| `executions` / `execution` | Project membership and actor ownership; `stop_any` is required for other actors |
| `admit_execution` / `stop_execution` | Explicit execution/delegation or stop authority, immutable source, policy revision and durable audit intent |
| Catalog metadata/grants and PG read/write/connect | Denied to governed actors until UC09.3/UC09.5 integration |
| Deploy, import/export, backup/restore, deletion | Denied to governed actors until their complete authority checks are integrated |
| Workload launch, logs, artifact downloads and WebSockets | Denied to governed actors until the isolated runtime and per-execution transport are integrated |
| Any unknown command, extra context field, unsupported API version or credential channel | Rejected; no local-owner fallback |

The exhaustive Rust [wire inventory](../../crates/local/src/authorization/inventory.rs)
classifies every daemon request. Every legacy request listed below is
**operator-only**, using the OS-owned private control socket. No authenticated
project command accepts or forwards an arbitrary legacy request:

- Identity/policy administration and catalog service configuration.
- Binding resolution, project create/attach/adopt/list, and register project.
- Console open/overview/actions, including source edits, uploads, downloads,
  ingestion, environments, notebook controls and analytical queries.
- Notebook transport/heartbeat, including WebSocket capability checks.
- Application API actions, including every nested catalog, project apply,
  SQL, branch, export, snapshot and ingestion command.
- Status/pending, raw operation/branch lookup, submit, rename, selection,
  connection credentials, leases, shutdown and process authorization.
- Offline installer, backup/restore and source/package CLI commands remain
  OS-operator operations and are not exposed by the authenticated adapters.

Adding a new daemon request requires updating the exhaustive boundary match.
Adding a project command requires explicit role, mutation and handler matches.
The legacy local console never treats an identity cookie as an owner launch
credential. The preview remains loopback-only; it is not shared network ingress.

## Immutable source and execution intent

Governed SQL/notebook edits create immutable, content-addressed revisions in the
control database, with a compare-and-swap head per logical asset. The digest
binds deployment, asset, kind and exact content. Requests are bounded to 32 KiB
of source. An asset name is a logical key, never a filesystem path. These edits
do not modify the operator's local checkout or files used by local-owner workers.

Execution admission selects an explicit immutable revision; it does not select
“latest” at execution time. Delegation approval binds the same revision and named
service UUID. Editing code produces a new revision with no inherited delegation
approval. A previously approved revision remains readable and can be admitted
only with its original bytes and current actor/service authority.

An admission records actor, effective principal, deployment, source revision,
policy revision and a unique ID. **It is an intent, not a running kernel or an
execution lease.** Responses state `runtime_started: false`. No current local-owner
worker consumes these records. UC09.4 must reauthorize intent at actual launch,
mount the admitted immutable bytes, attach the isolated execution lease, and
implement cancellation of real workloads. Package deployment and data movement
remain denied until their downstream controls exist.

## Transactions, races and migration

Schema 17 is additive and preserves schema 16 identities/sessions and existing
project ownership. Existing deployments get policy revision 1 with no external
roles or grants. A trigger initializes the same empty policy for new deployments.
Existing roots require the normal backed-up stopped-cell upgrade; ordinary
startup refuses implicit migration.

Every policy/source/admission mutation requires a bounded request key and the
expected policy revision. Policy changes advance that revision. Group membership
and principal disabled-state changes invalidate saved plans conservatively across
all project policies. Source writes additionally require the expected asset head.

Idempotency records are scoped by deployment and authenticated actor. Reusing a
key with changed input fails. Replaying the same input returns the original
receipt without repeating effects, after current action-specific permission
checks; a receipt cannot restore revoked access. The original admission receipt
is not a live status response: use `execution` to inspect later cancellation.

Policy, source/admission state, mutation receipts and audit intent commit in one
SQLite transaction. Failure to persist policy or audit rolls back the mutation.
The audit contains realm, project, actor/effective principal, checked policy
revision, action, request key and target ID/hash; it contains no bearer token,
provider secret or source contents.

Identity introspection runs outside the writer in UC09.1's bounded workers.
The writer revalidates the session before authorization, then evaluates current
policy and commits without releasing its single-writer boundary. Revocation and
configuration changes during provider I/O cannot commit with an obsolete context.

## Operator and client use

Use `identity policy-admin --request-file PRIVATE_JSON` on the operator's host.
For example, grant an existing principal an editor role on one deployment:

```json
{
  "action": "set_role",
  "deployment": "DEPLOYMENT_UUID",
  "subject": {"kind": "principal", "id": "PRINCIPAL_UUID"},
  "role": "editor",
  "expected_policy": 1,
  "key": "initial-editor"
}
```

The operator commands are `policy`, `audit`, `set_role` and `set_grant`. A group
subject uses `kind: group`. Null role removes an assignment. `set_grant` adds or
removes `execute`, `stop_any` or `act_as` using `present`; `act_as` requires an
existing service identity and source revision. Only `act_as` accepts those two
fields; the others use null. All mutations require `expected_policy` and `key`.

Authenticated users can submit `{"action":"projects"}` through the CLI or MCP.
The CLI reads command/session files with the existing private-file checks.
Browser clients submit the same JSON to `/auth/v1/control` with the session's CSRF
token. Source save fields are `deployment`, `asset`, `kind`, `contents`,
`expected_head` (null initially), `expected_policy` and `key`. Admission requires
`deployment`, `source_revision`, optional `effective_principal`, `expected_policy`
and `key`. No request can supply a local worktree path or actor identity.

## Verification

Policy tests exercise conflicting users/roles, project-list filtering, cross-project
IDs, default denials, explicit execution and stop grants, service confused-deputy
attempts, immutable approvals after edits, stale policy/source heads, actor-scoped
idempotency, group revocation/restart and audit/policy storage failures. Closed
wire-contract tests reject unknown actions and extra client context fields.

Process tests use the actual daemon plus CLI, MCP and browser adapters, including
CSRF/origin rejection, source edits and revocation. The extended
[Keycloak qualification](../../e2e/native/identity/qualify.py) exercises two real
TLS/OIDC users with different roles, admission/replay, stop ownership, denied data
paths and service execution approval before/after an edit. Recovery tests cover
schema 16-to-17 and all supported predecessor interruption boundaries.

UC09.3 is next: UC identity mapping and governed grants. This slice makes no
claim of running isolated workloads, revoking already-open data handles, or
qualifying shared-server/installed-release governance.
