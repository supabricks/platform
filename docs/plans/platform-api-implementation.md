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

**Project means the logical package.** A Supabricks installation is deployed
once. A project is a named package of resources inside it: databases and their
branches, analytical tables, notebooks, jobs, catalog publications and access
policy. Nothing is deployed, started or stopped per project.

The platform stores this as three records: `project_definitions` (identity and
name), `deployments` (the definition applied to a target, which is where
authorization is attached today) and the runtime `projects` row (the owner of
branches). The API exposes one resource. `Project.id` is the definition ID and
`Project.name` is the definition's name. The deployment and runtime rows are
internal and never appear in a path, a schema or an error. Authorization is
expressed per project and resolved to the internal record by the server.

The schema allows one definition to have several deployment rows. The API
assumes exactly one per project in an installation. AP01.1 decides whether to
enforce that with a constraint or to keep the rows as an internal detail of
`project apply`.

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
| Governed | Linux, every PR that touches `api/` or the server; nightly otherwise | L4, plus L1 to L3 under a signed-in session |

The governed run reuses the identity provider fixture and server setup from
`e2e/native/iam` and `e2e/native/governed-console`. AP00 measures how long that
takes on a hosted runner and decides whether it can run on every PR.

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
| AP00.8 | Governed transport and the L4 authorization generator | Open. Built with AP01, which is the first domain with roles to test |

Acceptance: `GET /api/v1/server/version` is served, described, routed, tested
at L0 to L3, L5 and L7 on Linux and macOS, and gated in CI.

### AP01: Project

**Completeness review.** This is the result of step a, from reading the code.
It corrects three `x-backend` values in the first draft of the specification.

| Operation | Today | Finding | Proposed |
|---|---|---|---|
| `listProjects` | Console lists projects (`console/projects.rs`); governed lists by role | One entry per project definition | Version 1 |
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

**Design questions to settle in AP01.1**

- Whether to enforce one deployment row per project definition, and how
  existing project-scoped authorization moves from the deployment to the
  project without changing who can do what.
- What a `path` is on a governed server, and who may see it.
- What deleting a project does to backups and to data other projects have
  already read.

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
`complete`; #285, #287 and #314 are closed; #286 and #315 remain open and
linked from the deferred operations.

### AP02 to AP08: PostgreSQL

Each follows the loop in section 2. Their completeness reviews are written when
the slice starts, because they depend on reading the code at that time. What is
known now:

| Slice | Mostly exists | Main gaps |
|---|---|---|
| AP02 Databases | Create, list, delete, suspend, resume, connection details | Compute sizes (#244), idle timeout (#245), storage and connection counts (#246), pooled connections (#252) |
| AP03 Branches | Create at head, time or log position; rename, default, expiry, suspend, resume, delete | Reset (#256), schema diff (#255), restore window (#254), per-branch size (#246) |
| AP04 SQL | One statement, cancel, saved queries | Multiple statements, plans, history (#261) |
| AP05 Tables and objects | Tables and columns; bounded preview | Paged reads (#257), batched edits (#258), reviewed DDL (#259), full introspection (#260) |
| AP06 Imports | Inspect, map, load, history, cancel, retry | Append and replace (#311), remote sources (#312), direct analytical import (#313) |
| AP07 Administration and observability | Little | Roles (#251), password reset (#250), extensions (#253), metrics (#247), query activity (#248) |
| AP08 Backups | Whole-cell offline bundle | Online backups, schedule, restore in place (#296) |

AP02 and AP03 are mostly a REST surface over capabilities that exist, so they
are the fastest. AP07 and AP08 are mostly new capability and are the slowest.

## 7. Risks

| Risk | Mitigation |
|---|---|
| The governed server only serves a network listener with a qualification receipt, which makes it expensive to start in CI | AP00.6 measures it. If it is too slow per PR, L4 runs on PRs that touch `api/` or the server, and nightly |
| Two HTTP APIs exist during the transition | The action endpoint is frozen: no new actions are added to it once AP00 lands |
| A domain's review finds the draft specification wrong, as AP01 did three times | Step a always runs before step b, and corrects the specification before anything is built on it |
| Capability work in step c is large and stalls a domain | Operations can be deferred individually; a domain completes with what version 1 needs |
| The console moves to the new API at a different pace | The console adopts a domain only after it is `complete`, using types generated from the bundled specification |
| Release qualification is by exact archive | Completing a domain is a source-level gate. Installed-archive qualification stays a separate step, as it is for every other workstream |
