# UC02: project-owned catalog metadata

UC02 adds a versioned metadata surface and default OSS Unity Catalog namespaces.
All calls require a validated, selected project deployment. The existing
`catalog` PostgreSQL discovery command is unchanged; the new surface is
`catalog metadata`, API action `catalog_metadata`, MCP tool `catalog_metadata`,
and console workspace command `catalog_metadata`. The browser Data UI follows
in UC06.

## Authority and identity

The local control database owns namespace and asset ownership. UC properties
never establish ownership. A default catalog alias contains a readable project
prefix and the full deployment UUID; its `analytics` schema has a separately
recorded UC UUID. Two deployments of the same project definition have different
runtime project IDs, aliases and UC objects. Defaults are created only by an
explicit, project-bound `ensure_namespace`, which later publication flows can
invoke lazily. Reads do not create namespaces.

Namespace creation records intent before each remote write. Catalog and schema
creation have separate durable checkpoints. Existing names without recorded
UUIDs are collisions. A crash or ambiguous write response leaves an
`indeterminate` namespace requiring explicit reconciliation; restart does not
adopt the object by name or by descriptive properties. A confirmed catalog
checkpoint can resume schema creation. Changes to recorded catalog/schema UUIDs,
parent, alias or provider metastore make the binding stale. UC02 offers no
rename, deletion, ownership transfer or destructive repair operation.

Metadata assets record owner project/deployment, stable local asset UUID,
provider instance, resource key, incarnation, quoted alias, schema, publication
revision, source revision, observation time and lifecycle state. Live PostgreSQL
identity is fenced by branch UUID, relation OID, database OID and relfilenode;
DDL and storage rewrites may invalidate an observation. Rename and ordinary
schema changes retain the asset ID when the storage incarnation is unchanged,
but invalidate its version. Drop/recreate creates a different incarnation.
Restoring or replacing underlying database state is not an identity migration;
that remains part of UC07 reconciliation.

A live PG table and its immutable Delta snapshot have separate asset IDs. The
snapshot projection uses the existing complete local publication descriptor,
epoch and manifest digest. It reports `provider=supabricks_snapshot`, **not** an
OSS UC table registration. `uc_object_id` is null until UC03 adds durable UC
publication. Snapshot provenance includes publication ordinal, source revision,
epoch UUID and snapshot timestamp; storage paths and credentials are omitted.

## API v1

Request schema: [catalog metadata command](../../schemas/catalog-metadata-command-v1.schema.json).
Response schema: [catalog metadata response](../../schemas/catalog-metadata-response-v1.schema.json).

| Command | Result |
| --- | --- |
| `capabilities` | Supported providers, limits and unsupported storage/governance operations |
| `health` | UC service health, including unavailable versus healthy/empty |
| `namespace` | Current owned namespace; revalidates recorded UUIDs when ready |
| `ensure_namespace` | Lazily creates and records the selected deployment's default namespace |
| `list` | A page of live tables and the current complete local analytical snapshot for a branch |
| `describe` | Refreshes one project's recorded asset and its schema |
| `resolve` | Requires the observed `expected_version`; never substitutes a renamed/recreated object |
| `validate_source` | Checks the expected revision and case-insensitive table/column collisions before later publication |
| `poll` | Observes an asynchronous request belonging to this deployment |

`list`, `describe`, `resolve`, `validate_source` and remote namespace operations
return an operation UUID with `state=running`. Poll until `complete` or `failed`.
A completed result contains a page, asset or namespace, plus catalog health.
Failures carry a typed code and retryability; raw provider responses and tokens
are never exposed. The CLI polls automatically; `catalog metadata poll ID`
resumes observation if its bounded wait expires. Namespace journals survive a
daemon restart; transient read-request IDs do not.

Each list page is pinned to the owner, branch and the observed asset/version set.
A changed set invalidates the cursor rather than mixing pages. Resolution is a
metadata check; it issues no storage credential or reader lease. UC04 will add
session-pinned data access. Case-sensitive PostgreSQL identifiers are rendered
with proper quoting; colliding analytical aliases are rejected by validation.
The legacy export path remains unchanged: UC03 must invoke validation before
registering an analytical publication in UC.

## Bounds and failure isolation

Two metadata workers serve the installation independently of the daemon writer.
SQL uses the existing bounded read-only query path (10-second statement timeout,
45-second connection/query ceiling and its result/frame limits). Metadata covers
up to 128 tables and 128 columns per table, with pages of 1–100 assets and a
2 MiB transport ceiling including overhead. The latest 32 request results are
retained for at most five minutes; capacity can evict older completed requests.
Active work is never evicted.

UC requests have 750 ms individual deadlines, bounded 64 KiB responses, no
redirects or inherited proxy, and no automatic retries. Namespace work performs
a fixed number of health/read/create calls, with at most two creation stages.
All credentials stay in private referenced files. External mode uses the UC01
verified-TLS and expected-metastore configuration. The service never enumerates
or adopts another project's UC objects.

Catalog unavailability is distinct from an empty dataset list. Existing live PG
and local snapshot metadata can still be inspected with catalog health reported
separately. Failed/partial source observations cannot invalidate the last
complete set. Final commits recheck branch revision and snapshot availability.
A catalog outage does not suspend PostgreSQL or block its ordinary SQL workers.
This remains a local OS owner workflow, not tenant isolation or multiuser RBAC.

## State and rollout

Control schema 14 adds `catalog_namespaces` and `catalog_assets`; it rewrites no
existing project, binding, PostgreSQL object or UC object. Existing schema-13
roots require the established explicit, backed-up installed upgrade. Startup
refuses an old schema instead of automatically migrating it. Upgrade tests cover
interruption at migration and activation boundaries and preserve the old backup.
UC/JRE inventory compatibility remains conservative; UC07 owns expanded catalog
upgrade/reconciliation support.

The alpha.28 candidate carries schema 14. Native qualification reuses immutable
alpha.24 engine inputs with the current installed binary and UC closure; it is
separate from complete archive qualification. The UC01 alpha.27 archive pipeline
assembled both archives but failed subsequent qualification setup because its
scripts still selected alpha.26 filenames; UC02 corrects the defaults. It is not
a qualified release. No production website change or
user demo data-root migration is included.
