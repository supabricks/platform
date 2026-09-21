# UC09.3: private UC identities and reviewed grants

UC09.3 implements the IAM00 private principal broker for the managed local Unity
Catalog provider. Catalog discovery is available through the authenticated
project-control CLI, MCP and loopback browser adapters. UC itself checks ordinary
principal requests. [UC09.4](uc094-isolated-execution.md) adds opt-in isolated
execution; shared ingress, PostgreSQL access and interactive output streams
remain gated by later governance slices.

## Principal and credential boundary

The broker derives its internal subject from the installation realm UUID and
stable platform principal UUID: `p-<realm>-<principal>@supabricks.invalid`.
External email, display name and provider subject cannot select a UC identity.
Mapping records bind that subject to UC's returned user UUID and provider UUID.
Every request verifies the same enabled UC user, subject and external ID. A
recreated platform identity gets a different subject and no inherited grants.
This UC pin soft-deletes users and refuses same-subject recreation; disabled,
missing or mismatched mappings deny access.

Only the managed local provider is supported. Its verified runtime trusts the
internal issuer alone and binds both listeners to loopback. The broker signs
ordinary RS512 access credentials with the existing private UC signing material,
using maintained `jsonwebtoken`/RSA libraries. Credentials expire within 60
seconds and no later than the authenticated platform session. They stay inside
the worker; neither clients nor workloads receive them. This is not direct OIDC
federation and does not expose a token-exchange endpoint.

The metastore admin credential is used only for provisioning and administrative
identity/object/grant checks. Actual catalog metadata is fetched with the user's
credential, then checked against the admitted publication UUID, revision, table
UUID, and immutable publication definition. Responses omit storage paths and
credentials. An admin metadata response is never a fallback for a denied user
request. Direct UC clients and user host logins remain outside the profile.

## Grant authority and reconciliation

UC is the effective catalog permission authority. Platform records are reviewed
administrative intents with distinct origins, not a cached allow decision.
An origin selects a principal or platform group, a retained publication revision
and an explicit set of immutable table UUIDs. It materializes `USE CATALOG`,
`USE SCHEMA`, and `SELECT` for enabled, mapped members. No project role, parent
namespace, new publication, or recreated table automatically inherits sharing.

Direct and group origins are unioned before computing changes. Removing one
origin never revokes access still justified by another. Provider reconfiguration/restart/key rotation, group membership and
principal-disable changes advance the governance revision and close new catalog
reads until reconciliation. Unmapped group members prevent resolution.

The operator reviews a `plan` containing the observed direct UC privileges,
desired privileges and origin records, then applies its content-addressed ID.
`plan` with an empty change list reconciles existing intents after group changes,
drift, outages or restart. Every application:

1. Checks the local plan revision and durable request key.
2. Commits the intended origins, audit event and an `applying` deny state before
   contacting UC. Other plans and in-flight reads become stale.
3. Verifies the reviewed remote identities and grants still match.
4. Applies removals before additions, using conditional object/principal UUIDs.
5. Verifies the complete resulting managed grant set, then commits `ready` and
   the successful receipt. Any failure leaves access closed.

A lost reply after a UC mutation is unresolved, never assumed successful. A new
reviewed reconciliation observes the partial result and completes the durable
intent. Replaying a successful apply key returns its historical receipt; it does
not reopen access or bypass the next live UC check. Changed keys/plans cannot
silently reuse another mutation's receipt. Startup marks interrupted operations
failed and keeps catalog access closed until a fresh reconciliation succeeds.

Every catalog read compares live managed privileges at metastore, catalog,
schema and table levels with the administrative intent, before and after fetching
user metadata. Unexpected extra or missing privileges, provider outage, changed
UUIDs or changed publication definitions close admission. There is no allow
cache. The single writer checks the current governance revision, principal,
session existence, epoch and expiry again before delivering worker results.

Grant operations are restricted to the trusted OS operator in this slice.
Console grant administration remains UC09.7. Privileged out-of-band edits are
outside the managed change path; they trigger drift rather than becoming an
implicit sharing decision. Already-delivered metadata cannot be recalled;
workload leases and termination remain UC09.4/UC09.6 work.

## Conditional UC mutation extension

The pinned fork adds `X-Supabricks-Object-Id` and
`X-Supabricks-Principal-Id` to permission PATCH requests. If either is present,
UC requires both, a single principal change, and exact resolved UUID matches
before applying anything. It then changes permissions using those same resolved
UUIDs. This prevents a name from selecting a recreated table or user between
review and mutation. Existing clients without these headers retain their prior
behavior. [UC fork PR #4](https://github.com/supabricks/unitycatalog/pull/4) and
[component pins](../../components/unity-catalog-source.lock.json) identify the
reviewed source; the dependency lock changes its source binding only.

## Interfaces

Operator commands use private JSON files:

```text
supabricks identity catalog-admin --request-file COMMAND.json --data-dir ROOT
```

- `{"action":"status"}` reports governance revision, state, recent plans and
  principal mappings.
- `{"action":"map_principal","principal":"PLATFORM_UUID"}` provisions an
  ordinary UC user. It records intent first and never adopts an existing name.
- If provisioning committed remotely but its reply was lost, inspect UC using
  the private operator interface, then explicitly approve its UUID with
  `{"action":"resolve_principal","principal":"PLATFORM_UUID","expected_uc_id":"UC_UUID"}`.
  Resolution checks the subject/external ID and leaves grants closed for review.
- A grant plan uses `{"action":"plan","changes":[{"publication":"PUBLICATION_UUID",
  "publication_revision":1,"subject":{"kind":"group","id":"GROUP_UUID"},
  "tables":["UC_TABLE_UUID"],"present":true}]}`. `present:false` removes that
  origin; an empty change list reconciles current intents.
- Apply with `{"action":"apply","plan":"PLAN_SHA256","key":"REQUEST_KEY"}`.

Authenticated clients use the existing `identity control` command, MCP
`project_control`, or browser `POST /auth/v1/control`, with the same cookie/Origin/
CSRF and credential-channel checks as UC09.2:

```json
{"action":"catalog","command":{"action":"list","search":"orders"}}
```

`describe` takes `publication`, `table` and `publication_revision`; inaccessible
or unknown objects return `item:null`. Lists/search return only UC-authorized
items and no counts/cursors derived from hidden assets. Project membership is
neither required nor sufficient for a separately shared publication. Credentials
must carry `project:control`; it does not itself confer any catalog rights.

The first adapter is bounded to 64 mapped principals, 128 metastore/namespace/table
objects and 512 origin records. Workers share the existing four-job admission
limit, have a 20-second operation deadline, reject redirects and ambient proxies,
and bound each response. Unsupported/oversized state fails closed.

## Migration and qualification

Schema 18 preserves schema-17 project roles, policy revisions and identity
sessions. It adds only principal mappings, grant origins, reviewed plans, audit
and governance state. Migration creates no remote grants. Existing roots require
the backed-up stopped-cell upgrade; ordinary startup cannot migrate them.

The UC fork change does not change H2 schemas, but **installed releases still
require qualification of the changed UC component**. The existing exact-backend
and component-inventory upgrade gates remain in force. An old UC metastore is
not silently adopted under this source pin. UC09.8 must qualify that release
transition before shipping a governed installed upgrade.

The portable tests cover origin overlap, unresolved group members, stale
publication revisions, recreated identity labels and migration/recovery. Two
real-UC tests run the pinned source-built Java server and the actual Rust broker:
ordinary principals are denied metadata and admin creation by UC itself; hidden
assets stay absent from platform list/search; grant changes invalidate reviewed
plans; a proxy loses provisioning and second-grant replies after UC commits; reconciliation
recovers that partial state across a Store reopen; outage and out-of-band grants
deny reads; in-flight revocation prevents result delivery; recreated table UUIDs
and disabled/reused UC subjects are refused. The fork's five permission tests
include conditional-grant rejection without side effects.

Run the real-server cases with:

```sh
SUPABRICKS_UC093_RUNTIME=/absolute/path/to/verified/uc-runtime \
  cargo test --locked -p supabricks-local --lib catalog_governance_real_uc -- --ignored
```

The catalog-probe workflow runs these cases on Linux and macOS. They qualify
identity/grant/metadata behavior. A Linux qualification also logs two equal-email
users in through real TLS Keycloak and routes their authenticated CLI requests
through the managed broker; disabled IdP accounts are denied. These checks do
not qualify isolated data execution or an installed
release. UC09.4 is next: connect admitted principals and revisions to the isolated
runtime and exact file mounts.
