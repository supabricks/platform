# Local OSS Unity Catalog service

UC01 adds the catalog service to the complete analytical distribution. It uses
our source-built OSS Unity Catalog, a private Temurin 17 JRE and independent H2
metadata. No Java, SBT, Maven, Docker or network download is needed on first start.
The PostgreSQL-only profile does not include this service.

`supabricks up` starts the local catalog automatically in an installation that
includes it. `supabricks status` includes a separate `catalog` section.

```bash
supabricks up
supabricks catalog service status
supabricks catalog service restart
supabricks down
```

The managed service binds two adjacent dynamically selected IPv4 loopback ports.
Supabricks validates authenticated readiness and rejects an endpoint that allows
anonymous catalog listing. Catalog failure does not change PostgreSQL readiness
or stop existing database workflows. Status reports `starting`, `ready`,
`degraded`, `backoff` or `failed`, with a bounded error code and no credentials.
After three launch attempts, automatic recovery stops until an explicit service
restart or a new daemon generation. Fix the reported problem before restarting.

State lives under the private data root in `catalog/`: H2 metadata in `etc/db`,
signing keys and the administrative service token in `etc/conf`, rolling server
logs in `etc/logs`, and bounded process output in `process.log`. These are
installation infrastructure, independent of any user project's database. The
bootstrap service token is daemon-only and is never returned to the console or
included in process arguments. Project ownership and user-facing metadata APIs
arrive in UC02; this slice does not register project assets or add a catalog UI.

The process uses the existing durable PID/start-identity ownership mechanism.
`down` also fences owned catalog processes after a daemon crash. Ambiguous or
reused PIDs fail closed; Supabricks never signals an unrelated process to free a
port. Restart preserves the provider and metastore identities. A changed
metastore identity fails readiness instead of silently adopting an empty store.

To invalidate credentials signed by the current local key:

```bash
supabricks catalog service rotate-key
```

Rotation stops the owned service, regenerates its key pair, and restarts it with
the existing metadata and grants. Old tokens become invalid. Durable rotation
intent permits recovery if interrupted. Ordinary restart creates a new bootstrap
token but does not revoke older tokens signed by the same key. This is a local
owner bootstrap, not an external identity-provider login or multiuser RBAC flow.

UC server logs rotate at 5 MiB with three archives; process output keeps one
bounded 5 MiB tail and is truncated in place by the supervisor. Keep raw logs
private. Catalog data is retained on `down` and on provider changes.

## Operator-managed OSS UC

Supply an explicit origin, a private token-file reference and the expected UC
metastore UUID. Remote endpoints require HTTPS with certificate verification.
The built-in WebPKI roots are used by default; a PEM CA bundle can be specified
for a private deployment. HTTP is allowed only on loopback.

```bash
chmod 600 /absolute/path/uc-token
supabricks catalog service configure external \
  --endpoint https://catalog.internal:8443 \
  --token-file /absolute/path/uc-token \
  --metastore-id 00000000-0000-4000-8000-000000000001 \
  --ca-file /absolute/path/private-ca.pem
supabricks catalog service status
```

Use the operator's actual metastore UUID. Endpoint credentials, query strings,
custom API paths, redirects and proxy inheritance are rejected. The token must
be a regular file owned by the current user with no group/other permissions.
Health checks reread token and CA references, so operators can rotate their
contents without restarting Supabricks. Missing or rejected credentials make
the provider unavailable; there is no anonymous or local-provider fallback.

External mode checks authentication, the catalog API shape and metastore
identity. It does not start, stop, rotate keys, migrate or copy the operator's
service state. Local backups may retain provider configuration and file
references; they do not capture the external metadata database or referenced
secrets. Cross-host storage reads and credential vending remain disabled.
Databricks-hosted compatibility is not a supported mode.

Switch back to the retained local metastore with:

```bash
supabricks catalog service configure local
```

## Source development and qualification

Development binaries without a release manifest do not start UC implicitly.
Build the reviewed artifact first and select it explicitly after starting the
development daemon:

```bash
python components/build-unity-catalog.py --source /path/to/unitycatalog \
  --tools /tmp/uc-build-tools --output /tmp/uc-runtime
supabricks catalog service configure local --runtime /tmp/uc-runtime
```

Installed binaries reject runtime overrides. Both assembly and service startup
validate the reviewed source/dependency pins and artifact inventory. The builder
downloads checksum-pinned inputs, builds only tracked source, and resolves SBT
dependencies from its private file-only Maven mirror. Runtime Maven coordinates,
hashes, declared licenses, POM ancestry and embedded notices are preserved alongside
the JRE legal files.

The native service fixture uses the current binary and UC artifact over the
immutable alpha.24 engine baseline. It tests installed discovery and lifecycle
offline on Linux x86_64 and macOS arm64; it does not replace full installed-release
qualification. The complete release workflow separately assembles alpha.27 with
the catalog closure. UC07/UC08 retain responsibility for catalog-aware upgrade,
recovery and complete release acceptance. Existing alpha.25/26 qualification
debt is unchanged. Automatic compatibility guesses across UC/JRE inventories are
rejected.

Local files remain accessible to the same OS owner independently of UC grants.
Governed multiuser storage requires the later IAM/isolation work. See the
[UC00 capability report](../architecture/uc00-catalog-probe.md) and
[UC implementation plan](../plans/unity-catalog-implementation.md).
