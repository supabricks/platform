# UC09 governed on-prem implementation plan

Status: UC09.0 / IAM00 merged in #66; UC09.1 authentication foundation merged in #67; UC09.2 project policy/admission implemented for review; UC09.3–UC09.8 remain open. Baseline 2026-09-21: UC08 #64 merged as
`c44fab5`; Linux/macOS alpha.34 local-owner qualification is complete.
[Architecture and threat model](../architecture/uc09-governed-on-prem.md) ·
[UC workstream](unity-catalog-implementation.md) · [Status](status.md).

UC09 remains one product milestone, divided below into reviewable slices. These
IDs expand UC09; they do not imply nine new features beyond its original IAM and
isolation requirement. UC09.0 is the previously referenced IAM00 foundation.
No governed mode is enabled until the final acceptance gate passes. Foundational
identity plumbing can ship behind a disabled profile while preserving local-owner
behavior. This document authorizes no automatic migration of an existing cell.

## Target and release contract

Confirmed first target: a single shared Linux x86_64 server, on-prem OIDC, managed OSS
UC, private local Delta storage and isolated execution. Keep Linux/macOS laptop
mode unchanged. Keycloak is the first authentication fixture; OCI/gVisor is the
first execution candidate. Pin and qualify these components before adoption.
No Kubernetes or hosted Databricks compatibility is in scope.

Completion requires two users and a service principal using the real console,
CLI/MCP, SQL and notebooks with distinct access. A user without permission cannot
reach protected metadata, source data, storage, another session or credentials,
even with arbitrary Python. Permissions remain correct through disable/revoke,
provider outage, restart, backup/restore and upgrade. Explicitly unsupported
entry points must be inaccessible, not silently privileged.

## Repository map

| Repository | Existing integration points | New responsibility |
| --- | --- | --- |
| `supabricks/platform` | `crates/local/src/{api.rs,client.rs,cli.rs,mcp.rs,daemon.rs}`, `console/server.rs`, `store/`, `projects/`, `project_apply.rs` | Principal context, identity/session storage, project authorization, policy journals and authenticated server ingress |
| `supabricks/platform` | `catalog/{adapter.rs,reads.rs,datasets.rs,publication.rs,runtime.rs}`, `engine/{sql.rs,mod.rs,exports.rs}`, `analytics.rs` | UC effective-principal checks, PG roles, admitted dataset file sets, copy/export boundaries and lease revocation |
| `supabricks/platform` | `notebooks/runtime.rs`, `supervisor/`, `python/{notebooks,analytics,ingest}`, `recovery.rs`, `upgrade.rs`, `components/`, `install/native/` | Isolated execution adapter, source-pinned dependencies, installer/preflight and exact-release evidence |
| `supabricks/console` | Auth/API client, project membership, Data and notebook/environment controls | Login, explicit role/grant controls, session identity and revocation UX; platform advances the reviewed gitlink |
| `supabricks/unitycatalog` | Pinned `AuthService`, authorizer, permission/user APIs and publication endpoints | Only the identity/group/delegation gaps demonstrated by UC09.0; source/API regression tests and platform pin update |
| `supabricks/sail` | UC provider and Spark Connect session boundary | Only demonstrated credential/path isolation gaps; prefer launcher/network/mount controls before engine patches |
| `supabricks/neon`, `supabricks/postgres` | Existing PG17 engine | No storage rewrite or engine fork migration; use native roles/grants and lifecycle controls |

Allocate new migrations from main at implementation time. Suggested platform
modules `identity/`, `authorization/`, `execution/` and `audit/` are proposals;
keep the portable core free of OS/runtime-specific dependencies.

## Slice sequence

| Slice | Deliverable | Prerequisites |
| --- | --- | --- |
| UC09.0 / IAM00 | Executable identity/isolation feasibility and frozen contract | UC08 |
| UC09.1 | Stable principals, sessions and on-prem login | UC09.0 |
| UC09.2 | Project/execution RBAC and audited admission | UC09.1 |
| UC09.3 | UC principal mapping and governed grants | UC09.0–UC09.2 |
| UC09.4 | Isolated notebook/Sail runtime and scoped file access | UC09.0; integrate UC09.2–UC09.3 before acceptance |
| UC09.5 | Governed PG, ingestion, publication and package/data movement | UC09.2–UC09.4 |
| UC09.6 | Bounded revocation, audit operations and governed recovery | UC09.1–UC09.5 |
| UC09.7 | Complete console administration and user workflow | UC09.1–UC09.6 |
| UC09.8 | Exact installed-release qualification and demo | All preceding slices |

UC09.4 feasibility can be explored alongside identity work after UC09.0; shipping
an isolated runtime does not qualify governance until the other controls are
integrated. Each slice may have coordinated repo PRs. Split further where a
single review cannot cover its changes, without dropping acceptance criteria.

## UC09.0 / IAM00 — Prove the boundary first

Implemented: [capability report and chosen design](../architecture/iam00-governance-probe.md),
[reproducible harness](../../e2e/native/iam/README.md). Direct external UC federation
and native UC groups are no-go at the pinned version; the private broker and
per-execution OCI/gVisor candidate support proceeding to UC09.1. The product
governed profile remains disabled.

Build a developer-only Linux harness using the pinned product and UC fork, a
pinned on-prem Keycloak fixture and a reviewed OCI/gVisor candidate. Do not expose
an existing local cell on the network or change its identity mode.

Deliver a capability report, source/configuration hashes, threat model and go/no-go
record covering:

1. Alice, Bob and a service identity map to stable IDs. Equal/changed emails,
   deleted/recreated subjects, forged issuer/audience and an external `admin`
   label cannot acquire another principal's rights. Demonstrate non-admin UC
   metadata filtering and denied table reads. Determine whether native UC groups
   qualify or materialized direct grants are needed.
2. A real managed notebook with native Python dependencies queries Sail inside
   the proposed sandbox. A second principal cannot read its files, processes,
   sockets, credentials, caches or authorized Delta files. Deny direct control,
   UC admin, PG and raw path access outside the admitted set.
3. A restricted PG connection cannot use another branch or owner/control role.
   Prove server-file/program access and privilege escalation are denied.
4. Prototype kill/expiry of a running query/kernel, including daemon loss and an
   already-open file. Establish feasibility of the 60-second execution revocation
   and five-minute IdP-disable targets; do not infer them from token TTL alone.
5. Measure idle footprint, notebook startup, 10/100 MB reads, native-package
   compatibility and concurrent-user resource consumption. Propose explicit
   supported concurrency and resource budgets from these measurements.

Acceptance: reproducible positive and negative checks with bounded diagnostics,
no leaked processes, one chosen identity/group and runtime design, and a recorded
no-go for unsupported paths. Record required UC/Sail patches and component pins.
A design document or successful OIDC login alone does not complete IAM00.

## UC09.1 — Principals and login

Implemented for review: [identity/session architecture and qualification](../architecture/uc091-principals-login.md). Schema 16 preserves local-owner IDs. OIDC/PKCE, private sessions, explicit bootstrap, groups, scoped service credentials and authenticated CLI/MCP/browser identity previews are implemented. Product ingress remains disabled; integration with product authorization and the complete console follows in UC09.2–.8.

Add installation realm, stable principals, issuer/subject mappings, disabled state,
service identities and initial platform-managed groups. Migrate local-owner state
without inventing remote grants. Reserve bootstrap administration for an explicit
operator action; no first-browser-user-wins promotion.

Implement browser authorization-code/PKCE with protected server-side sessions,
logout and key rotation; authenticated CLI/MCP flows; service-account credentials
with bounded scopes. Introduce actor/effective-principal context and audit events
from the first slice. Shared network ingress remains gated until isolation is
qualified. Define authenticated request versioning for the separate console.

Acceptance: identity collision/recreation, replay, issuer/audience, CSRF, redirect,
expired/revoked session, token leakage and local-owner regression checks. IdP
unavailability must not create an implicit local-owner session.

## UC09.2 — Project and execution authorization

Implemented for review: [project policy and admission](../architecture/uc092-authorization.md).
Schema 17 adds deployment roles, separate execution/service-use/stop grants,
immutable source revisions and transactional policy/audit/idempotency records.
CLI, MCP and browser control requests share the authenticated handler. Admission
records are intents only; data access, package/data movement and workload/stream
routes remain denied until UC09.3–.5 supply their required boundaries.

Build one capability-checking path for every control API, including list/detail,
logs, artifact downloads, WebSockets, source edits, imports, deployment, restore,
backup and deletion. Add project roles and explicit execution/`act_as` grants.
Project membership alone grants neither catalog nor PG data access. An actor may
only stop another actor's session with a separate administrative permission.

Bind execution to immutable admitted source/package revision and effective
principal. Editing shared code must not cause it to run as a more privileged
owner. Persist policy revision and audit intent with idempotent mutations.

Acceptance: a route/action coverage inventory with default denial for new actions;
two users with conflicting project roles; tampered project IDs and stale plans;
service-principal confused-deputy tests; failures to persist audit/authorization
state deny the mutation. Permission checks apply equally to console and agents.

## UC09.3 — UC identities and grants

Implement the UC09.0-selected principal adapter. Keep metastore provisioning and
user data access separate; short-lived user credentials never carry admin
privileges. Add reviewed grant/revoke operations, group handling, drift detection
and reconciliation without creating a second effective catalog grant authority.
Keep object UUID/incarnation and publication-revision fences.

Acceptance: UC itself denies unauthorized metadata/table operations; platform
lists/search do not leak names/schema/counts from inaccessible assets. Prove
recreated users/objects, overlapping group grants, partial grant failure,
provider outage, out-of-band drift and grant changes between plan and execution.
New sessions cannot use an unresolved grant; never fall back to an admin read.

## UC09.4 — Runtime and storage isolation

Integrate the qualified OCI runtime behind a platform execution adapter. Isolate
Jupyter server/kernel, Sail session, ingestion parsers and executable package preparation, with
read-only admitted source/data mounts and private scratch/output/cache. Treat
native extensions as untrusted code. Enforce per-lease network, process, disk,
CPU/memory limits and cleanup independent of notebook cooperation.

Acceptance: direct Python file/network access, symlink and Delta-path escapes,
other users' processes/sessions, inherited descriptors, host sockets, environment
secrets, cache poisoning and fork/resource exhaustion fail to cross the boundary.
Real Spark/Arrow notebooks and installation paths with spaces still work. Losing
the sandbox capability or renewal channel fails closed; no host-process fallback.

## UC09.5 — PG and all data-copy paths

Provision explicit per-principal branch roles/credentials, restrict backend
reachability and close sessions on expiry. Keep control SQL credentials private.
Separate PG read/write/DDL authority from project control and catalog consumption.
Authorize whole-source branch exports and clones before invoking the current
broad frozen exporter; reject partial/RLS sources outside the supported profile.

Cover ingestion, migrations, fixtures, publication, `.sbproj`/`.sbdata`, branching,
backup/restore, environment hooks, notebook outputs and saved query results.
Reconcile destination roles/grants and rotate copied credentials before admitting
users to a clone or import. Derived publications need explicit sharing authority;
packages carry requirements, not grants or runnable foreign identities.

Acceptance: restricted users cannot reach control roles, server files/programs,
other branches or copied source credentials. Denied transfers publish no data or
active revision. Source/destination grant races fail safely; transactional import,
crash recovery and current single-user format guarantees remain intact.

## UC09.6 — Revocation, audit and recovery

Finish authoritative renewal, denial propagation, process/connection termination
and result-stream closure. Test platform revocation separately from IdP account
disable. Record acknowledged deny time and last successful access; include open
file descriptors, cached data and in-flight work. Privileged out-of-band changes
must not leave an unbounded stale allow decision.

Provide bounded audit retention/export and disk-full behavior. Recover policy
journals and stop orphan leases after crashes. Governed backups are administrative
assets; restore starts closed, reconciles with current identity/policy state and
rotates sessions/credentials. Cross-realm restore requires explicit remapping;
no automatic reactivation of historical grants. Existing local-owner backups
remain compatible within their original profile.

Acceptance: finalized revocation bounds pass under worker/daemon/provider failure,
clock skew and restart. Backup rollback cannot resurrect access. The audit trail
correlates actor/effective identity, source revision, data revision and result
without credentials or user content. Document what trusted host admins can alter.

## UC09.7 — Console workflow

Add sign-in/out, identity/session display, project membership, service-principal
use, dataset grants, actionable denials and revoke/terminate controls. Extend the
current project creation, import, Data, SQL, notebook and package flows; no manual
UC CLI or hand-edited grant files in the normal demonstration. Show unsupported
modes clearly; do not advertise local-owner mode as multi-user isolation.

Acceptance: browser test with independent Alice/Bob sessions demonstrates project
creation, ingestion/publication, denied discovery, reviewed sharing, a bound
Spark query/notebook, revocation during execution and a correlated audit event.
Server checks remain authoritative for crafted API requests. Preserve the
single-user reconnect and offline workflows.

## UC09.8 — Qualify the complete governed release

Add governed reports to the existing R04 collector, tied to exact platform,
console, UC/Sail, IdP and sandbox source/configuration/image hashes. Preserve all
local-owner Linux/macOS release gates and add the supported Linux shared profile;
macOS receives no implicit governed qualification.

Exercise the real installer, explicit administrator setup, TLS ingress, two users
and a service principal, private storage, upgrade/restore and clean shutdown.
Internet access is denied while the explicitly configured on-prem IdP and internal
services remain reachable. Include native-package, grant-race, bypass, revocation,
audit failure and resource-limit cases, with independent concurrent sessions.
Ship an installed shared-server demo and operator recovery/update guide. Record
retries and limits rather than converting implementation presence into a pass.

Acceptance: every inherited and governed report passes on each advertised exact
archive; resource budgets and revocation targets from UC09.0 are met; no leaked
processes or broad credentials; unsupported endpoints remain unreachable. Only
then enable the governed profile and mark UC09 complete.

## Immediate next action

Review and qualify UC09.2, then implement UC09.3 UC identity mapping and governed
grants. Integrate the same authenticated context and policy revision with the
private principal broker. Keep shared ingress and actual workload launch disabled
until the complete authority, isolation and release gates pass.
