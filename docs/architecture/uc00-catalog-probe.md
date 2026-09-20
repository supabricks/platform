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
hash sidecar. Build tools download dependencies; runtime qualification runs in
an OS sandbox that permits only loopback networking. CI performs the build and
probe independently on Linux x86_64 and macOS arm64.

The harness creates its own private cell, projects, PG tables, Delta epochs,
UC state, principals and Sail processes. It never connects to an existing
Supabricks daemon. Raw diagnostics and credentials stay in the private temporary
root; only allowlisted evidence is uploaded. Failed roots are retained locally
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
