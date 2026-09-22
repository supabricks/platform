# UC09.5 — Governed PostgreSQL and data movement

This slice adds authenticated data commands to the private control API. Shared
network ingress remains disabled until UC09.8. It does not expose PostgreSQL
listeners or credentials to users. The operator must explicitly grant data
capabilities and start the selected branch. Existing unenrolled local-owner
branches and portable package formats keep their behavior.

## Authority and PostgreSQL execution

`authorization::Command::Data` uses the same authenticated CLI/MCP/browser
context as the earlier slices. Project membership is necessary, but even a
project administrator has no implicit data authority. Operator policy commands
assign principal/group capabilities on an exact deployment and branch:

| Capability | Authority |
| --- | --- |
| `read` | Bounded SQL results from the whole supported branch |
| `write` | One SQL data mutation; no DDL or returned row payload |
| `ddl` | Restricted branch schema administration |
| `copy_source` | Whole-source frozen export, logical data export or clone |
| `receive` | Import into the branch, or create a child of that branch |
| `share` | Publish a frozen export as a managed snapshot |

These grants are separate from UC table SELECT, execution grants, `act_as`, and
project roles. SQL runs as the authenticated actor; this API does not accept an
arbitrary effective identity. Group changes, principal disable and policy edits
advance the existing deployment policy revision. Both admission and commit check
the exact revision, current session, principal, branch identity and revision.
There is no automatic SQL retry. `find` by the original request key and `status`
by operation ID allow the actor to inspect a lost response. Data-bearing results
require fresh authorization, including the originating session. Other actors
cannot retrieve them.

Each transaction receives a new randomly credentialed role bound to its actor
and selected branch connection. Ordinary roles are non-owner, NOINHERIT,
NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION and NOBYPASSRLS. DDL uses an
explicit SET ROLE membership in a restricted NOLOGIN branch owner. User SQL
never executes on the control connection. The role password stays inside the
worker; neither the response nor the journal contains it.

The extended protocol admits one statement. A first-keyword allowlist closes
transaction control, role administration, COPY PROGRAM, procedural blocks and
session SET. PostgreSQL privileges enforce object access. PUBLIC schema,
table/column and sequence privileges are removed; `set_config` is also revoked
so expressions cannot remove deadlines. Internal Neon schemas and the engine
health-check table are excluded from user grants. The supported source profile
is ordinary public-schema tables, without RLS, foreign tables, views, user
routines or user triggers. Engine-owned Neon objects are explicitly recognized
and remain private. Unsupported sources fail before broad exporter authority.

Workers hold mutations in a PG transaction while the sole writer rechecks
policy and records the commit authorization. Loss of this handshake rolls back.
Statement, idle transaction and transaction deadlines are 2, 5 and 10 seconds;
the worker has an independent bounded deadline and explicitly terminates the
session before dropping its role. Server transaction deadlines also protect a
lost daemon. The API limits SQL to 32 KiB, reads to 200 rows/32 KiB, four workers,
one transaction per branch, and 512 journal records. Journal retention and
operational policy are part of UC09.6.

Enrolling a branch retires its legacy application login. On every PG admission and
compute restart, reconciliation removes old login passwords and terminates old
sessions, resets the restricted owner and its memberships, and reapplies the
closed privilege baseline. The control credential remains private. Governed
branches cannot return a legacy owner connection URI. The existing loopback-only
backend and UC09.4 networkless sandbox remain the network boundary; direct
external PG clients and sandbox PG networking are not enabled by this slice.

## Copy and activation boundaries

| Path | Enforcement |
| --- | --- |
| Frozen PG → Delta export | Whole-source capability before native submission; source profile checked again on the frozen child before `pg_read_all_data`; original authorization checked on each exporter tick |
| Managed snapshot publication | Fresh `copy_source` and `share` authority, checked again in the atomic snapshot-head commit; an operator retry cannot bypass an originating user's fence |
| PG branching | Same-project child requires `copy_source` plus `receive` on its parent; the child remains quarantined until copied logins are disabled, credentials are reconciled and source policy is checked again; no data grants are copied |
| `.sbdata` export | Whole-source copy authority even when selecting fewer tables; ordinary restricted PG role, one read-only snapshot, existing typed format validation |
| `.sbdata` import / typed data ingestion | Explicit destination `receive` and `ddl`; existing non-executable schema and COPY-text validation; fresh tables and the complete table set in one transaction; final destination policy fence |
| SQL migrations and fixtures | Explicit DDL/write commands or typed data import; legacy project-apply/migration/fixture runners remain operator-only |
| `.sbproj` | Existing source-only declarations and requirements; no grants, sessions or foreign execution identities; notebook outputs remain stripped; unpacking does not bind or execute a project |
| Native ingestion parsers and environment hooks | Legacy host paths remain operator-only; authenticated work cannot dispatch these through the data API. Untrusted parser/package code remains in the UC09.4 sandbox |
| Notebook output and query results | UC09.4 results remain actor-private with fresh UC/policy checks; new PG results use the same actor/policy fence; no automatic publication of outputs |
| Private saved queries, backup and restore | Legacy routes remain operator-only and cannot be selected through an authenticated data command |

The authenticated logical-data transport accepts up to 30 KiB of `.sbdata` bytes
as `archive_hex` and returns at most that size. This is an RPC limit; the existing
local-owner `.sbdata` v1 format and 32 MiB data limit are unchanged. Cross-project
physical clone, row/column-limited source export, arbitrary SQL scripts,
privileged schema extensions, raw uploads and automatic project activation are
not admitted through this boundary. UC09.7/UC09.8 must qualify the shared product
flows before removing their existing gates.

Copy permits stay attached to native export operations, including across retries
and crashes between submission and journal mapping. Missing mappings fail
closed. A revoked export is stopped and its unpublished staging data cleaned up.
A denied publication cannot advance a snapshot head. A clone has no inherited
platform grants, and source/control passwords cannot authenticate to its new
endpoint. Logical packages contain data/provenance, not PG roles or grants.

Schema 20 adds separate data grants, branch enrollment and operation journals.
Upgrade from schema 19 requires the existing stopped, verified backup workflow;
no data authority is granted by migration. Restart marks uncommitted work
interrupted and a lost commit acknowledgment uncertain, without replay. An
uncertain SQL commit requires inspecting the destination; it is never reported
as a confirmed rollback. PG supplies atomicity for the complete import table set.
Governed restore/revocation reconciliation is described in [UC09.6](uc096-revocation-recovery.md); shared restore
admission stays closed.

## Example and qualification

An operator grants a capability using `identity policy-admin` with a private
request file containing, for example:

```json
{"action":"set_data_grant","deployment":"DEPLOYMENT_UUID","branch":"BRANCH_UUID","subject":{"kind":"principal","id":"PRINCIPAL_UUID"},"capability":"read","present":true,"expected_policy":2,"key":"grant-read-1"}
```

The authenticated principal uses `identity control --session-file SESSION
--request-file REQUEST` with:

```json
{"action":"data","deployment":"DEPLOYMENT_UUID","command":{"action":"sql","branch":"BRANCH_UUID","capability":"read","sql":"SELECT * FROM example","expected_policy":3,"key":"read-1"}}
```

`e2e/native/governed/qualify.py` exercises the candidate daemon with the pinned
native PG17 engine and real exporter. It covers two principals, separate grants,
control/file/role denials, timeouts, revocation during a write, atomic `.sbdata`
transfer, credential isolation on a real clone, frozen publication races, and
writer death before commit. It records the candidate binary and test-source
hashes and requires owned-process cleanup. The Linux catalog CI gate runs it.
Portable tests cover policy/session fences, audit-before-effects, restart
journals, package rejection of foreign authority and schema-19 migration.
