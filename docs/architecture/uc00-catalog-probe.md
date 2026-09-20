# UC00: OSS Unity Catalog feasibility probe

Status: implementation and qualification in progress. This is a developer probe,
not an installed catalog feature. See the [UC00–UC09 plan](../plans/unity-catalog-implementation.md).

## Scope and inputs

Build OSS UC v0.6.0 from the controlled `supabricks/unitycatalog` fork. Exact
source/API/build/JDK/JRE hashes live in
[`components/unity-catalog-source.lock.json`](../../components/unity-catalog-source.lock.json).
The candidate includes one Supabricks patch: bind the URL transcoder to IPv4
loopback. Upstream's Armeria API already binds loopback; its front-end transcoder
did not. The probe checks actual listening sockets.

The baseline is the previously qualified alpha.24 distribution, containing PG17.8,
SeaweedFS and Sail 0.7.1 built from `supabricks/sail` source. Its CI artifact IDs
and ZIP hashes are pinned in
[`baseline.lock.json`](../../e2e/native/catalog/baseline.lock.json).
Reusing that immutable baseline avoids rebuilding unchanged engines for the
catalog experiment. This does not qualify alpha.25/26 or resolve their existing
release failures.

## Run the probe

Use Python 3.12 or later for the harness. The runtime uses its own checksum-pinned
Temurin 17 JRE; it does not use a host JVM or a Spark JVM plugin.

```bash
python components/build-unity-catalog.py --source /path/to/unitycatalog \
  --tools /tmp/uc00-tools --output /tmp/uc00-runtime
python -m pip install -r e2e/native/catalog/requirements.txt
python e2e/native/catalog/probe.py --release /path/to/qualified-alpha24 \
  --uc-runtime /tmp/uc00-runtime --report /tmp/uc00-report.json
```

The source checkout must be clean at the lock's commit. The output must be new.
The build produces a server-only archive, its build/JAR inventory and an archive
hash sidecar. Build tools download dependencies; runtime qualification runs with
external outbound networking denied. Linux uses a loopback-only network namespace;
macOS uses Seatbelt outbound restrictions and verifies service listeners on loopback. CI performs the build and
probe independently on Linux x86_64 and macOS arm64.

The harness creates its own private cell, projects, PG tables, Delta epochs,
UC state, principals and Sail processes. It never connects to an existing
Supabricks daemon. Raw diagnostics and credentials stay in the private temporary
root; only structured evidence and bounded, redacted failure diagnostics are uploaded. Failed roots are retained locally
for investigation. Successful runs remove their state.

## Decisions awaiting qualification

- Use independent embedded H2 metadata for the local-owner profile. Back up the
  stopped UC `etc` state, including metadata, policies and signing keys; retain
  analytical files separately. This is not a live or power-loss backup test.
- Keep canonical UC registrations unique. UC rejects overlapping registered table
  locations. A consumer project should bind an existing table identity rather
  than re-register its location under another catalog.
- Evaluate native UC resolution and frozen, version-pinned session views against
  two complete PG export epochs. UC03/UC04 must provide publication-set atomicity.
- Test real UC metadata permissions separately from data access. Same-owner local
  files and broad SeaweedFS credentials cannot establish multiuser isolation.
- Use explicit Sail catalog configuration. Synthetic principal JWTs signed with
  the private test server key exercise authorization only; they are not an IdP
  implementation. Production bootstrap belongs to UC01 and IAM integration.
- The UC server source is Apache-2.0. The bundled Temurin JRE is GPL-2.0 with the
  Classpath Exception, with its legal directory preserved. JAR notices remain
  embedded. UC01 must finish the production dependency lock and license inventory;
  this probe records resolved JAR hashes and is not a reproducible-release claim.

Measured results, capability limitations and the UC01 go/no-go decision will be
recorded here after both native reports pass.

## Observed capability contract

The following results have passed locally on Linux; the final native evidence
must also pass before UC00 is marked complete.

| Capability | Observed behavior | Integration decision |
| --- | --- | --- |
| Source-built native provider | Sail reads actual PG-exported Delta files through UC; list/describe work | Reuse the existing provider; no Spark JVM plugin |
| SQL names | Three-part `provider.schema.table` works; four-part `provider.catalog.schema.table` is rejected by Sail | Configure one explicit provider entry per UC catalog, with a fixed `default_catalog` |
| Metadata permissions | Two principals receive different catalog access; denied reads return 403; expired tokens return 401 | Keep authentication enabled; no anonymous fallback |
| Revocation/expiry | New named reads in an existing uncached Sail session fail after revocation/expiry | Explicitly disable all provider caches for this profile; cached-provider policies are unqualified |
| Object incarnation | Drop/recreate changes the UC table ID and removes old grants | Bind IDs and revisions, not just names |
| Duplicate registrations | A second registration at the same location is denied, including across catalogs | Consumer bindings reference the canonical registration |
| Table rename | Standard REST table PATCH returns 500 and leaves the original object unchanged | Do not expose native table rename; fix error mapping separately if needed |
| Local credentials | File-backed temporary-credentials request returns 200 without storage credentials | File access remains OS-owner access |
| Local data denial | A principal denied metadata can still read known paths as the same OS user | No multiuser storage-security claim |
| Mutable publication names | A two-table query returns orders total 30 and payments total 10 after only orders advances | Never resolve each logical table independently against mutable names |
| Frozen resolution | Views resolved once to epoch-1 paths and Delta version 0 return totals 10/10 after names change | UC04 needs a frozen-resolution adapter backed by complete publication manifests and leases |
| SeaweedFS | Explicit endpoint, path-style requests and static access keys read the exported Delta table | Keep this as a separately tested local-owner path; do not assume AWS STS |
| Native credential vending | UC can return its configured test credentials; Sail does not consume them automatically | A storage credential adapter is required before vending can be used |
| SeaweedFS test token | Forwarding the synthetic UC test session token fails; removing it permits the static-key read | This is not STS compatibility or downscoping; governed S3 access is unsupported |
| Storage bypass | A principal denied UC vending can read a known S3 location when given broad static keys | Do not hand these credentials to mutually untrusted users |
| Outage and recovery | New named resolution fails during UC outage; IDs, grants and reads survive restart and stopped-state restore | Use H2 plus a coordinated stopped-state backup for the initial local profile |

Frozen views are a **probe of the adapter design**, not a shipping security
boundary. They deliberately avoid further catalog resolution and can outlive a
catalog grant. UC04 must bound them by policy/credential lifetime and data leases;
UC09 additionally needs process and storage isolation. A single UC registration
must not imply that all tables in a publication have advanced atomically.

The test users and project definitions are fixtures. This slice does not install
project-to-UC ownership APIs, publication journals, dataset bindings, console
browsing, external IdP login or product RBAC. Those remain UC01–UC09 work.

## Follow-up implementation boundaries

UC01 should bundle and supervise this server-only closure, with its own H2 2.2.224
state, Temurin 17.0.20.1+1 runtime and loopback endpoints. Complete the transitive
build dependency lock, third-party license inventory, runtime inventory validation,
auth bootstrap/rotation and authenticated readiness checks before shipping it.
The required fork patch is [unitycatalog #1](https://github.com/supabricks/unitycatalog/pull/1).

UC02/UC03 should maintain stable platform asset identities and one canonical UC
registration per immutable publication table. Keep display names separate from
physical names and remote object incarnations. Publication commit and retention
remain platform responsibilities. UC04 should resolve an entire committed manifest
once, validate its UC IDs/locations, acquire references, and install version-pinned
session aliases. The probe uses temporary views and integer-column fixtures;
it does not qualify the production adapter, concurrent publication journal,
all PG types, schema evolution, malicious metadata or non-default Sail caches.

UC09/IAM must qualify a real credential issuer, storage enforcement and execution
isolation before mutually untrusted users share a deployment. UC's legacy static
`sessionToken` setting is explicitly test-only and cannot supply that boundary.
There is no Databricks-hosted dependency or fallback in this design.

The stricter macOS network policy is scoped to this probe. The earlier shared
release sandbox allowed the external TCP sentinel; existing macOS disconnected
release claims need requalification with an enforced policy before UC08. This is
additional to the alpha.25/26 release failures already recorded in the plan.

Source references: [UC authentication/bootstrap](https://docs.unitycatalog.io/server/auth/),
[pinned UC server configuration](https://github.com/supabricks/unitycatalog/blob/9c4b48ccbf18ffd89b4dfe966e79a2b6cb826069/etc/conf/server.properties),
[pinned Sail UC provider](https://github.com/supabricks/sail/blob/9544c9253e981a82c5f9e493c43ce98a4d9d41b7/crates/sail-catalog-unity/src/provider.rs).
