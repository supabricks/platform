# Platform API: implementation plan

[Implementation plan index](README.md) · [API specification](../../api/README.md)

*Status: proposed · Date: 2026-10-10 · Slice prefix: AP*

The platform gains one versioned REST API, described by the OpenAPI document in
`api/`. The console, the command line, MCP agents and customer automation all
use it. This plan says how each part of that API goes from a line in the
specification to a tested, regression-protected capability, one domain at a
time, starting with Project and then PostgreSQL.

The specification was derived from the console prototype
(`supabricks/console`, `prototype/`). It contains 24 domains and will contain
roughly 250 operations. About 40% of the prototype's features have no platform
support today; those are tracked as `Console backend:` issues #244 to #316.

## 1. What "complete" means

A domain is complete when all of the following hold. Nothing is marked complete
on the strength of the specification alone.

1. **Scope is decided.** Every operation in the domain is either in version 1
   or explicitly deferred, with a reason and an issue.
2. **The contract is frozen.** The domain's paths and schemas have been
   reviewed, carry examples, and declare the role each operation needs.
3. **The platform can do it.** Any missing capability is implemented in the
   store, daemon and typed actions, with its own unit and crash tests.
4. **The REST surface exists.** Each operation is routed and calls the same
   typed action the command line and MCP use.
5. **The tests in section 4 pass** against a running server, in every profile
   the domain supports.
6. **It is locked.** The domain is listed as complete in `api/status.yaml`,
   which turns on the regression gates for it in CI.
7. **The books are closed.** Issues it resolves are closed, new gaps found on
   the way are filed, and the delivery ledger records the evidence.

## 2. The loop for one domain

Each domain goes through the same seven steps. Each step is one pull request
unless noted, so a domain is typically five to eight pull requests.

| Step | Work | Output |
|---|---|---|
| a. Completeness review | Read the code behind every operation. Correct `x-backend`. Decide what is in version 1. | A table in this plan, corrected spec, new issues |
| b. Contract freeze | Add examples, `x-required-role`, error codes per operation. Review. | Spec PR; domain stage `frozen` |
| c. Capability | Close the platform gaps: migrations, store, daemon, typed actions. One PR per capability. | Rust PRs with unit and crash tests |
| d. REST surface | Handlers for the domain on the shared router. | Rust PR; stage `implemented` |
| e. Tests | Scenario tests, authorization matrix, parity check for the domain. | Test PR (may be combined with d) |
| f. Run and fix | Boot the server, run every layer, fix what fails, repeat. | Green CI on both profiles |
| g. Close | Mark complete, close issues, update the ledger. | Stage `complete`; gates on |

Stages are recorded per domain in `api/status.yaml`:
`draft` → `frozen` → `implemented` → `complete`. An operation can be `deferred`
individually, which removes it from the gates and from the published contract.

## 3. Architecture decisions

These were agreed on 2026-10-10. AP00 implements them.

**One implementation, several adapters.** REST handlers are thin. Each parses
and validates a request, calls an existing typed action (`api::Action`,
`authorization::Command` and their peers), and maps the result. The command
line and MCP keep calling the same actions. No business logic lives in the HTTP
layer. Where a typed action does not exist, step c adds it first.

**Routed on the daemon, served by the existing HTTP servers.** The platform
has two HTTP servers: the local console server (`console/server.rs`, one per
launched console) and the governed sign-in server (`identity/transport.rs`).
Neither holds state; both forward to the daemon, which is the single writer. So
the API has two parts:

- `crates/local/src/rest/` runs on the daemon. It owns the route table, the
  handlers and the problem mapping, and has no HTTP dependency.
- Each HTTP server is a transport. It checks the session, origin and CSRF
  token, frames the request as a `rest::Call`, sends it to the daemon and writes
  the `rest::Reply` back.

The local console server mounts it at `/api/v1`, next to the current
`POST /api/workspace` action endpoint. Both run until the console has moved to
`/api/v1`; then the action endpoint is removed. The governed server gains the
same mount when the first domain needs authorization tests.

**No web framework by default.** The server uses `hyper` directly today, and
the repository checks its dependency boundary. The plan adds a route table and
a small path matcher, not `axum`. If routing by hand becomes a burden, adding a
framework is a separate, explicit decision.

**The specification is the source of truth for routes.**
`api/scripts/generate.py` writes the server's route table
(`rest/routes.rs`) from the specification and `api/status.yaml`: one enum
variant and one route per operation that is `implemented` or `complete`. The
dispatcher matches on the enum exhaustively, so an operation marked implemented
without a handler does not compile, and CI fails if the generated file is out
of date. Nothing can be routed that the specification does not describe.

**Cross-cutting behaviour is implemented once**, in `rest/`:

| Concern | Behaviour | Backed by |
|---|---|---|
| Errors | `application/problem+json` with a stable `code` | Existing diagnostic codes, mapped in one table |
| Idempotency | `Idempotency-Key` required on creates and starts | The `key` the actions already take; a small table for actions that take none |
| Concurrency | `ETag` from `revision`; `If-Match` checked before the action | Existing `revision` columns and `expected_policy` |
| Paging | Opaque cursor over a stable order | Per-listing keyset queries |
| Long work | `202` with an `Operation` | Existing durable operations; a uniform view over the per-feature tables |
| Profiles | `403 unavailable_in_profile` | Existing gated-operation inventory |

**A project is one instance.** A Supabricks installation is deployed once. A
project is a named package of resources inside it: databases and their
branches, analytical tables, notebooks, jobs, catalog publications and access
policy. Nothing is deployed, started or stopped per project.

The platform stores this as three records. `project_definitions` is the
portable source (a folder with `supabricks.toml` and a fixed ID). `deployments`
is one live instance of a source in this installation; roles and grants are
attached here. The runtime `projects` row owns that instance's branches.

The API's `Project` is the instance. `Project.id` is the instance's ID (the
platform's deployment ID), its name is its own, and its roles are the ones the
platform already stores for it. The source it was created from is a field,
`source_id`. The same source can be created more than once, for example as
staging and as production; those are two projects with separate data and
separate access. The word "deployment" and the runtime project ID never appear
in a path, a schema or an error.

## 4. Test methodology

Tests run against a real server: a daemon and console started on a temporary
data directory, as `crates/local/tests/console.rs` and
`e2e/native/console_health.py` already do. Nothing is mocked below the HTTP
boundary.

The harness is Python, in `api/tests/`, alongside the existing `e2e/native`
Python suites. It uses `pytest`, `schemathesis` for tests generated from the
specification, and an OpenAPI response validator, with versions pinned by hash.

### Layers

| Layer | What it proves | How it is written |
|---|---|---|
| L0 Specification | The document is valid and has not changed incompatibly | `redocly lint`; `oasdiff` against `main` for `complete` domains |
| L1 Conformance | Every response matches its schema, status and headers | Automatic: a validating client wraps every request any test makes |
| L2 Scenarios | The domain does what it should | Hand-written per domain: lifecycle, each documented error, edge cases |
| L3 Invariants | Rules that hold for every operation | Generated from the spec: no session → `401`; missing CSRF → `403`; unknown ID → `404`; invalid body → `400`; missing `Idempotency-Key` → `400`; replayed key → same result; stale `If-Match` → `412`; errors are problem documents and leak no paths or stack traces |
| L4 Authorization | Each role can do exactly what it should | Generated from `x-required-role`: every operation is called as nobody, viewer, editor, administrator and an administrator of another project |
| L5 Durability | Work survives a restart | Per long-running operation: kill the daemon mid-operation, restart, find it by key, see it finish or fail cleanly |
| L6 Parity | REST and the command line agree | Per domain: change through one, read through the other |
| L7 Fuzz | Unexpected input is handled | `schemathesis` over the domain; a short run on every PR, a long run nightly |

L1, L3 and L4 need no per-domain code: adding an operation to the
specification creates its tests. L2, L5 and L6 are written for each domain in
step e.

### Coverage rules

- Every operation in a `complete` domain has at least one L2 scenario. Tests
  name the `operationId` they cover; a script fails CI when one has none.
- Every error `code` an operation documents is produced by at least one test.
- `scripts/coverage.py` continues to check that every prototype feature is
  served by an operation.

### Profiles

| Profile | Where it runs | What it covers |
|---|---|---|
| Local owner | Linux and macOS, every PR | L0 to L3, L5 to L7 |
| Governed | Linux and macOS, every PR | L4, plus L1 and L2 for signed-in callers |

The governed run needs no identity provider. Callers are service accounts,
which the owner of the machine creates and gives roles through the control
socket; `supabricks identity api` serves the API to their bearer tokens on
loopback. The daemon authenticates each token and checks the caller's role per
operation, the same path the governed console uses, so the run takes a few
seconds. What it cannot cover is anything only a person signed in through an
identity provider can do: today that is creating a project as an installation
administrator, which has a unit test instead.

### Regression protection

A new workflow, `.github/workflows/api.yml`, runs on every pull request:

1. Lint the specification.
2. Check that the generated route table and status table are current.
3. `oasdiff`: compare the base branch's protected operations (`frozen`,
   `implemented` or `complete`) with the change. They may gain optional fields.
   They may not lose or change anything a client could depend on.
4. Build the binary, start the server and run the whole suite, on Linux and
   macOS. This includes the rule that every served operation has a scenario
   test in its domain's file.

A domain's tests are selected by its stage, so marking a domain `complete` is
what makes its tests mandatory. From then on any pull request, in any
workstream, that breaks one of them fails CI. Reopening a domain requires
changing `api/status.yaml`, which is visible in review.

## 5. Tracking

- `api/status.yaml` holds the stage of each domain and the list of deferred
  operations. The domain table in `api/README.md` is generated from it.
- Each domain has one tracking issue with a checklist of its operations. The
  existing `Console backend:` issues are linked from it and closed in step g.
- `docs/plans/status.md` gains one row per completed slice, with the CI run
  that is its evidence.

## 6. Slices

| Slice | Scope | Depends on |
|---|---|---|
| AP00 | Foundations: router, shared behaviour, test harness, CI workflow, status file. One read-only endpoint end to end. | Agreement on section 3 |
| AP01 | Project: `projects` and `operations` | AP00 |
| AP02 | Databases | AP01 |
| AP03 | Branches | AP02 |
| AP04 | SQL: statements, saved queries, history | AP03 |
| AP05 | Tables and objects: paged rows, edits, introspection | AP04 |
| AP06 | Imports | AP03 |
| AP07 | Database administration and observability | AP02 |
| AP08 | Backups and restore | AP03 |
| AP09 onward | Sync, analytics, notebooks, environments, jobs, catalog, project definition, access, identity, secrets, alerts, usage, server | Ordered when AP08 is under way |

AP02 to AP08 are the PostgreSQL group. AP04 to AP08 do not depend on one
another and can proceed in parallel once AP03 is complete.

### AP00: foundations

No domain is completed in this slice. It builds what every domain needs.

| Part | Work | State |
|---|---|---|
| AP00.1 | `api/status.yaml`; generated README table; `x-required-role` in the conventions | Done |
| AP00.2 | `rest/` on the daemon with the generated route table and matcher; mounted at `/api/v1` in the local console server | Done |
| AP00.3 | Shared behaviour: problem errors and their mapping from platform errors | Done |
| AP00.3b | Idempotency, `ETag` and `If-Match`, paging | Moved to AP01, with the first operations that use them, so they are built against real tests |
| AP00.4 | Uniform `Operation` view over the existing operation tables | Moved to AP01; it is `listOperations` and `getOperation` |
| AP00.5 | Test harness in `api/tests/`: server fixture, validating client, generated L3 invariants, pinned dependencies | Done |
| AP00.6 | `api.yml` workflow with lint, generation check, `oasdiff` and the suite on Linux and macOS | Done; first hosted run is the evidence |
| AP00.7 | One endpoint end to end, `GET /server/version` | Done |
| AP00.8 | Governed transport and the L4 authorization generator | Done, with AP01 |

Acceptance: `GET /api/v1/server/version` is served, described, routed, tested
at L0 to L3, L5 and L7 on Linux and macOS, and gated in CI.

### AP01: Project

**Completeness review.** This is the result of step a, from reading the code.
It corrects three `x-backend` values in the first draft of the specification.

| Operation | Today | Finding | Proposed |
|---|---|---|---|
| `listProjects` | Console lists projects (`console/projects.rs`); governed lists by role | One entry per instance | Version 1 |
| `createProject` | `project create --key`; governed request table keyed by actor and key | Idempotency already exists. `start_from: sample` has no backing (#315) | Version 1 without `sample` |
| `getProject` | Available | | Version 1 |
| `updateProject` | No rename; no description field | Needs a migration and an action. Name lives in two tables | Version 1 (#287, #314) |
| `deleteProject` | Not available | The largest gap: must remove branches, analytical data, catalog publications, notebooks and bindings, and refuse while another project reads a publication | Version 1 (#287) |
| `getProjectDeletionImpact` | Not available | Read-only companion to delete | Version 1 (#287) |
| `startProject`, `stopProject` | `up` and `down` act on the whole cell | The installation is deployed and run once. A project has no runtime of its own, so the draft was wrong | **Remove.** Starting and stopping belong to the installation, in the `server` domain |
| `getProjectMap` | Console overview assembles it | Needs a typed action so REST does not depend on console code | Version 1 |
| `listProjectServices` | `status` and `doctor`, for the cell | Not per project | **Move** to the `server` domain |
| `restartProjectService` | Not available | Draft marked it command-line only; it does not exist | **Defer** |
| `listRecentItems` | Not available | Convenience, not core | **Defer** (#286) |
| `listOperations` | Per-feature status commands only | Needs one view over several tables, with filters (#285) | Version 1 |
| `getOperation` | `operation get ID` | | Version 1 |
| `cancelOperation` | Cancel exists per feature | Needs dispatch by kind | Version 1 |

That is ten operations in version 1, two removed, two deferred and one moved.

**Applied to the specification so far:** start and stop are removed with the
`state` field; services moved to `/server/services`; restart is deferred
(#318); `start_from` is replaced by `source_id`; every operation names its
required role; `x-backend` values are corrected.

**Built so far:**

- Catalog 32 makes project names unique without regard to case and records
  each project's description and creation time. `project create` and the
  console's import take a name; a name in use is refused with `name_taken`.
- `listProjects` and `getProject`, with paging, the name filter and `ETag`.
- `createProject`. It reuses the console's create path and returns the apply
  operation. To create a second project from an existing source, import it;
  that belongs to the project-definition domain.
- Idempotency, once for every domain: replies are stored by key in
  `api_idempotency`, a repeat returns the first reply, and a key reused for a
  different request is `409 idempotency_key_reused`.
- The operations view (#285). `operation_index` is one ordered feed with
  creation and finish times, kept current by triggers on the existing
  operation tables, so no writer had to change. It covers project applies and
  database and branch changes; other kinds join with their domains.
  `listOperations`, `getOperation` and `cancelOperation` are served. Only
  applies can be cancelled today.

- `updateProject`: rename and description, conditional on `If-Match`. The name
  now belongs to the project once it exists, so editing the label in a source's
  manifest no longer renames an existing project.

- `deleteProject` and `getProjectDeletionImpact`. The request hides the
  project and frees its name at once. A durable operation then cancels a
  pending apply, force-deletes branches leaf first and removes the folders the
  server created. Rows stay as a tombstone, as deleted branches do. It needs
  the current revision, the exact name and an idempotency key.
- Delete refuses with `delete_blocked` while the project has sync pipelines,
  publications, analytical data or running work, or while another project
  reads its data. Nothing reclaims those as part of a delete yet (#320), so
  refusing is the safe behaviour; the impact preview lists what blocks.
  `force` is removed from the contract until it has a defined effect.
- An engine-backed test server (`engine_api`) runs from an installed release.
  It proves that a created project finishes and that a delete removes its
  branches and folder. It is not wired into CI yet.

- The API for signed-in callers. `rest::Caller` is set by the daemon from how
  a request arrived and is never read from the wire. For a signed-in caller the
  router checks the role the specification declares (`x-required-role`) before
  the request is read: no role in the project is `404`, too low a role is
  `403`, and every refusal is written to the security audit. The role table is
  generated from the specification with the routes.
- The governed server and `supabricks identity api` serve `/api/v1` to service
  tokens. Browser sessions on a governed server keep using the console's own
  endpoints until the console moves to this API.
- On a governed server only an installation administrator creates a project,
  through the existing governed create path, and delete is not offered
  (`x-profiles: [local]`, `403 unavailable_in_profile`): delete is in the
  platform's list of governed operations that are not yet qualified.
- L4: 88 generated authorization tests, plus signed-in scenarios for both
  domains.

**Closed on 2026-10-11.** `projects` and `operations` are `complete`: ten
operations are served, tested at every layer and gated in CI. What was left
out, and where it is tracked:

| Left out | Why | Tracked |
|---|---|---|
| `getProjectMap` | Every node is a resource of another domain; built as those complete | #321 |
| `listRecentItems` | Convenience, not core | #286 |
| Delete while the project has sync, publications or analytical data; `force` | Nothing reclaims those as part of a delete yet; delete refuses instead | #320 |
| Delete on a governed server | Not yet qualified for the governed profile | #320 |
| A required name on the command line and console import | They still default to the source's name. Requiring it changes about seventy scripts and the shipped console | Open decision |
| `start_from: sample` on create | No sample tables exist | #315 |

**Design questions to settle in AP01.1**

- Decided: a project's name is chosen by the person at create or import, has
  no default, and is unique in the installation without regard to case.
  Creating a source a second time under a name already in use is refused with
  `name_taken`. The name is stored on the source today, so this needs a `name`
  column on the instance with a unique index, filled from the source for
  existing rows. Rename changes one instance only.
- Decided: `path` is shown to the owner of a local installation only. A
  governed server never returns it.
- Decided: deleting a project deletes its backups too. No per-project backups
  exist yet (#296), so the rule is recorded in the contract. A project another
  project reads from cannot be deleted until the reader is removed.

**Parts**

| Part | Work | Closes |
|---|---|---|
| AP01.1 | Contract freeze for `projects` and `operations`: the decisions above, examples, required roles, error codes | |
| AP01.2 | Description field; rename across definition and runtime records, keeping IDs and bindings | #314, part of #287 |
| AP01.3 | Project map as a typed action | |
| AP01.4 | Operations feed: one ordered view with kind, state, target and key filters, and cancel by kind | #285 |
| AP01.5 | Delete with impact: reviewed, refuses on dependents, crash-safe | rest of #287 |
| AP01.6 | REST handlers for the ten operations | |
| AP01.7 | Scenario, durability and parity tests; authorization matrix for the domain | |
| AP01.8 | Run, fix, mark complete, close issues, update the ledger | |

**Scenario tests (L2)**

- Create returns an operation; following it yields a project with one database
  called `main`.
- Creating twice with the same key yields one project. A different key with the
  same name is `409 name_taken`.
- Invalid names are `400` with a field error.
- List pages in a stable order and filters by `q`. A viewer sees only projects
  where they hold a role.
- Rename keeps the ID; a second rename with a stale `ETag` is `412`.
- Description round-trips, including empty and maximum length.
- Deletion impact counts match what was created.
- Delete without `If-Match` is `428`; with the wrong `confirm` is `400`; with a
  dependent project is `409` and lists it; with `force` proceeds.
- After delete, every resource of the project is `404` and its storage is gone.
- The map contains every database, pipeline and analytical table created.
- Operations list is newest first, filters by kind, state and target, and finds
  an operation by idempotency key.
- Cancel is `409` on a finished operation and moves a running one to
  `cancelled`.

**Durability tests (L5):** kill the daemon during create and during delete;
after restart the operation is found by key and reaches a final state, and a
half-deleted project is either fully present or fully gone.

**Parity tests (L6):** a project created through REST appears in
the command line's project listing; one created through the command line appears in
`listProjects`.

**Acceptance:** all ten operations pass L0 to L7 on the local profile on Linux
and macOS and L4 on the governed profile; `projects` and `operations` are
`complete`; #285, #287 and #314 close when the pull request merges; #286, #315,
#320 and #321 remain open and are linked from the contract.

### AP02 to AP08: PostgreSQL

Each follows the loop in section 2. Their completeness reviews are written when
the slice starts, because they depend on reading the code at that time. What is
known now:

| Slice | Mostly exists | Main gaps |
|---|---|---|
| AP02 Databases | Done; see below | Compute sizes (#244), idle timeout (#245), storage (#246), pooled connections (#252), governed changes (#322), cascade delete (#323) |
| AP03 Branches | Create at head, time or log position; rename, default, expiry, suspend, resume, delete | Reset (#256), schema diff (#255), restore window (#254), per-branch size (#246) |
| AP04 SQL | Done; see below | Multiple statements, plans, history (#261), governed servers (#328), folders (#329) |
| AP05 Tables and objects | Tables and columns; bounded preview | Paged reads (#257), batched edits (#258), reviewed DDL (#259), full introspection (#260) |
| AP06 Imports | Inspect, map, load, history, cancel, retry | Append and replace (#311), remote sources (#312), direct analytical import (#313) |
| AP07 Administration and observability | Little | Roles (#251), password reset (#250), extensions (#253), metrics (#247), query activity (#248) |
| AP08 Backups | Whole-cell offline bundle | Online backups, schedule, restore in place (#296) |

AP02 and AP03 are mostly a REST surface over capabilities that exist, so they
are the fastest. AP07 and AP08 are mostly new capability and are the slowest.

### AP02: Databases

**Completeness review.** From reading the code on 2026-10-11. The draft
described a database as a record of its own with compute, storage and a
default branch. The platform has none of that:

- **A database is a root branch.** There is no `databases` table. A database
  is a branch with no parent, and its branches are that branch's descendants.
  Its ID, name, state and PostgreSQL version are the root branch's. So the
  database's first branch carries the database's name, and a database name is
  unique among the branches of the project, not only among databases.
- **A project can hold several databases.** Nothing limits roots per project;
  the limit is 32 running branches per installation.
- **The default branch is one per project**, not one per database.
- **Lifecycle changes are branch changes** (`SetState`, `DeleteBranch`,
  `RenameBranch`), each a durable operation the engines carry out. Without
  engines nothing finishes.
- **Nothing recorded when a branch was created, and a rename did not move its
  revision**, because that revision fences lifecycle work.
- **Compute size, idle timeout, storage used and history retention do not
  exist.** Open connections through the gateway are counted, with a fixed
  limit of 64 per branch.
- **On a governed server every lifecycle request is reserved to the
  operator**, PostgreSQL is private, and nobody connects directly.

| Operation | Today | Decision |
|---|---|---|
| `listDatabases`, `getDatabase` | Branch listing; no root-only view | Version 1, over a new store view |
| `createDatabase` | `database create` | Version 1, local profile. Name only |
| `updateDatabase` | `branch rename` on the root | Version 1, local profile. Rename only; the default branch, size, idle timeout and retention are removed from the request |
| `deleteDatabase` | `branch delete` on the root, which refuses while it has children or is the default | Version 1, local profile, with those refusals as `delete_blocked` (#323) |
| `suspendDatabase`, `resumeDatabase` | `branch suspend` and `resume` on the root | Version 1, local profile |
| `getDatabaseConnection` | `connect`, which returns address and password together | Version 1. Address only; `available: false` on a governed server |
| `revealDatabaseCredentials` | The same `connect` | Version 1, local profile |
| `listComputeSizes` | Nothing | **Deferred** (#244) |

Nine operations are in version 1 and one is deferred.

**Applied to the specification:** `compute`, `storage`,
`history_retention_seconds` and `last_active_at` are removed from `Database`
until they exist (#244, #245, #246, #254); `state` is `provisioning`,
`running`, `suspending`, `suspended`, `resuming` or `deleting`;
`holds_project_default` says why a database cannot be deleted alone;
`postgres_version` is the major version; names follow the platform's rule (40
characters, letters, digits and hyphens); `Connection` has no `options`,
`pooled` or role choice (#251, #252) and gains `sslmode: prefer`; every change
is `x-profiles: [local]` (#322).

**Built:**

- Catalog 33 records when each branch was created and counts its renames and expiry changes, by
  trigger, so no writer changed. The API's revision is the lifecycle revision
  plus the renames, so it rises with both and `If-Match` works for a rename.
- `store/database_directory.rs`: databases as root branches with their state,
  branch count, connections and whether they hold the project default.
- `rest/databases.rs`: thin handlers that call the same branch actions the
  command line uses (`api::Action`), so there is one implementation.
- The router now carries what only the owner's machine has (engines and the
  connection gateway), which connection details need.
- Operations on a root branch are reported as database operations:
  `database.create`, `database.suspend`, `database.resume` and
  `database.delete`, with the database as `target`.
- A change repeated with the same key after the reply was lost finds the
  operation the first attempt started.

**Tests.** 61 scenario, durability and parity tests in
`api/tests/test_databases.py`, 54 generated role tests, and the generated
invariant and fuzz layers. Two of the scenarios need engines: a database
starts, accepts a real `psql` connection, suspends, resumes with its data, is
refused deletion while it has a branch, is deleted and frees its name; and a
connection wakes a suspended database. The `api` workflow gains an `engines`
job that runs every engine-backed API test on Linux and macOS with the
qualified engine archive, so they no longer run locally only.

**Left out, with where each is tracked:**

| Gap | Tracked |
|---|---|
| Any database change on a governed server | #322 |
| Deleting a database with its branches, or the one that holds the default | #323 |
| A default branch per database | With branches (AP03) and #323 |
| Compute sizes, idle timeout | #244, #245 |
| Storage used | #246 (open connections are now reported) |
| Pooled connections, choosing a role | #252, #251 |
| Per-person credentials on a governed server | #306 |

**Acceptance:** the nine operations pass L0 to L7 on the local profile on
Linux and macOS and L4 on the governed profile, and the engine-backed
scenarios pass in CI; `databases` is `complete`.

### AP03: Branches

**Completeness review.** From reading the code on 2026-10-11.

| Question | What the platform does | Decision |
|---|---|---|
| What can a branch start from? | The head of another branch, a past time, or a log position (`Mutation::BranchFrom`). The point is stored in `branch_pins` | All three served. The point is reported back as `created_from`, as asked, not as the position it resolved to |
| Can it start from any branch? | Only from one that has converged: not while it is starting, suspending or resuming | `409 conflict`. Without engines nothing converges, so real branches are made only in engine-backed tests |
| A point the platform does not hold | Found when the operation runs, not when it is accepted | Documented: the operation fails |
| Expiry | `SetTtl`, a future time or none. Not for the default branch. An expired branch deletes itself | Served on create and update. Also refused for a database's first branch, which the platform would allow: an expiring database is not something to offer by accident |
| Default branch | One per project, across its databases (`project_defaults`) | `setDefaultBranch` and `is_default` are project-wide and say so. A default per database is #326 |
| Delete | Refused for the default, for a branch with children, with open connections or work | `409 delete_blocked`. A database's first branch is deleted with `deleteDatabase` |
| Reset, restore window, schema diff | Do not exist | Deferred: #256, #254, #255 |
| Head position, changed bytes, creator, sync pipeline | Not recorded per branch | Removed from `Branch` (#246) |
| Signed-in callers | Lifecycle is reserved to the operator | Reads only; every change is `x-profiles: [local]` (#325) |

**Built:**

- `store/database_directory.rs`: `BranchView`, a branch with its database,
  parent, children, starting point, expiry and revision.
- The catalog 33 trigger also counts a change of expiry, so the revision rises
  with a rename, an expiry change and a lifecycle change.
- `rest/branches.rs`: eight operations. Suspend and resume share the database
  handlers' code (`databases::set_state`), so a repeat behaves the same.
- Operations on a branch are `branch.create`, `branch.suspend`,
  `branch.resume`, `branch.delete` and `branch.set_expiry`, with `result.branch`
  on success. Work on a database's first branch stays `database.*`.
- Connection details for a branch come from the database's `connection` and
  `credentials` operations, which already take a branch.

**Tests.** `api/tests/test_branches.py`: scenario, durability and parity tests
on the first branch and every refusal, the generated role tests for the eight
operations, and four engine-backed tests: a branch starts from its parent and
diverges, with a nested branch and deletes in order; branches start from a
past time and a log position and see only what existed then; a branch is
renamed, suspended, resumed and made the default; a branch deletes itself when
its expiry passes.

**Left out, with where each is tracked:**

| What | Issue |
|---|---|
| Branch changes on a governed server | #325 |
| A default branch for each database | #326 |
| Reset a branch to its parent | #256 |
| The span of history a branch can start from | #254 |
| Schema diff between branches | #255 |
| Changed bytes and head position | #246 |
| Deleting a database together with its branches | #323 |

**Acceptance:** the eight operations pass L0 to L7 on the local profile on
Linux and macOS, with the engine-backed tests run by the `engines` job.

### AP04: SQL

**Completeness review.** From reading the code on 2026-10-11. There was no
draft contract for this domain; it is written from what the platform does.

| Question | What the platform does | Decision |
|---|---|---|
| How does a statement run? | On one of four worker threads, as the application role, through the branch's gateway address, in one transaction (`query.rs`). The console starts it and polls a handle held in memory (`console::workspace::Queries`) | A statement is a resource: `runStatement` answers `201` with `state: running`, `getStatement` reads it, `cancelStatement` stops it. It is not an `Operation`: it is not durable and carries a result |
| What bounds it? | 32768 bytes of SQL, 1 to 1000 rows, 256 KiB of result, 100 to 30000 ms | The same, with defaults of 200 rows and 10 seconds. Over a limit the statement fails with `54000`; nothing is cut short |
| Writes | Read-only transaction unless asked | `read_only` defaults to true |
| More than one statement | PostgreSQL refuses it at prepare | The statement fails with `42601`. Scripts are #261 |
| How long is it kept? | Ten minutes after it finishes, 32 handles, gone on restart | Documented. `getStatement` answers `404`, and says a write may have committed. A history is #261 |
| Repeats | The console names each statement with an ID it makes up | `Idempotency-Key`. The statement ID is derived from the caller and key, so a repeat finds the first even if its reply was not stored |
| Four workers busy | Refused | `429` with `code: capacity_exhausted` |
| Values | Text, as PostgreSQL prints them; null for null | The same: nothing is lost to JSON numbers |
| Errors | SQLSTATE and message; no detail, context or statement text | `error: {sqlstate, message}` |
| Saved queries | Up to 100 per project, one private file each, with title, SQL, the branch and a revision. No folders, no timestamps | Five operations. `updated_at` is the file's modification time. Titles are counted in characters, not bytes. Folders and export are #329 |
| Signed-in callers | PostgreSQL is private on a governed server; nobody is handed a connection | The whole domain is `x-profiles: [local]` (#328) |

**Built:**

- `rest/sql.rs`: eight operations. Statements reuse the console's `Queries`
  under a scope of their own, so a console's handles and the API's are never
  mixed; the daemon passes it, and the count of other busy workers, in
  `rest::Local`.
- `console/workspace.rs`: the saved query file format and directory are shared
  with the API, so a query saved through either is the same file.

**Tests.** `api/tests/test_sql.py`: saved queries in full without engines
(contract, paging, revisions, the limit of 100, durability, and parity with
the file the console reads), statement refusals, the generated role tests, and
thirteen engine-backed tests: values of every type come back as text; a
statement is read-only unless asked; PostgreSQL's own codes for four failures;
a result over the limits fails and commits nothing; running out of time;
cancel; a repeated key runs once; the fifth statement is refused; a statement
is found only through its project; a statement wakes a suspended branch.

**Left out, with where each is tracked:**

| What | Issue |
|---|---|
| Several statements in one run, structured plans, history | #261 |
| Statements and saved queries on a governed server | #328 |
| Saved query folders, export to project source, who and when | #329 |

**Acceptance:** the eight operations pass L0 to L7 on the local profile on
Linux and macOS, with the engine-backed tests run by the `engines` job.

## 7. Risks

| Risk | Mitigation |
|---|---|
| The governed server only serves a network listener with a qualification receipt, which makes it expensive to start in CI | Resolved: role tests use service accounts and a loopback API server, and run on every PR |
| Two HTTP APIs exist during the transition | The action endpoint is frozen: no new actions are added to it once AP00 lands |
| A domain's review finds the draft specification wrong, as AP01 did three times | Step a always runs before step b, and corrects the specification before anything is built on it |
| Capability work in step c is large and stalls a domain | Operations can be deferred individually; a domain completes with what version 1 needs |
| The console moves to the new API at a different pace | The console adopts a domain only after it is `complete`, using types generated from the bundled specification |
| Release qualification is by exact archive | Completing a domain is a source-level gate. Installed-archive qualification stays a separate step, as it is for every other workstream |
