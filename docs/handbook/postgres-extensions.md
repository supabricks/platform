# PostgreSQL extensions

The local operator console/API exposes the extensions shipped by the running
compute's installed engine, their available and installed versions, and whether
the platform can enable or disable them. No extension is downloaded at runtime.
The managed set is `pg_stat_statements`, `pgcrypto`, `pg_trgm`, `uuid-ossp` and
`citext`. Vector and PostGIS are explicitly unavailable until separately bundled
and qualified. Older installations report absent modules as unavailable.

```sh
supabricks extensions list --branch main
supabricks extensions enable pgcrypto --branch main
supabricks extensions disable pgcrypto --branch main
```

Use the ordinary `--project` and `--data-dir` options where needed. The CLI reads
the branch revision and sends an explicit target. Console workspace actions and
local API v1 use the same typed envelope; MCP exposes the `extensions` tool:

```json
{"action":"extensions","command":{"action":"set","target":{"branch":"BRANCH_UUID","revision":1},"name":"pgcrypto","enabled":true}}
```

For listing, the inner action is `list`, with the same explicit target and no
name or enabled field. See [the command schema](../../schemas/extensions-command-v1.schema.json).
Responses include `shipped`, versions, `enabled`, `required`, `managed`,
`can_enable`, `can_disable`, `requires_restart`, schema, and analytical-sync
notes, including structured `unsupported_column_types`. Available files alone do not imply permission to manage an extension.

## Lifecycle and ownership

Only the five reviewed names can reach extension DDL. Neon runtime modules and
`plpgsql` are protected; other diagnostic extensions may be listed but cannot
be managed through this API. Newly enabled modules use the
`supabricks_extensions` schema owned by the private engine administrator.
Application roles receive schema USAGE, never CREATE. Use qualified functions,
for example `supabricks_extensions.digest(...)`, or explicitly include this
schema in the application's search path. Existing extensions in other schemas
or with different ownership are shown but never silently adopted.

Disabling uses `DROP EXTENSION ... RESTRICT`. PostgreSQL refuses removal while
a table, index or other object depends on the extension. The API exposes no
CASCADE, arbitrary version, schema, path, SQL, or download option. Repeating a
successful desired-state request has no additional effect. After a connection
loss or timeout, refresh the catalogue: the commit outcome can be uncertain.

Each request validates project ownership, branch revision, running state and
current compute readiness before using private control credentials. Listing
does not resume a suspended compute. Mutations are refused while capture or
an event trigger is active, preserving capture's DDL fence. Requests have a
four-second overall deadline, two-second statements and half-second lock waits;
errors expose SQLSTATE rather than private connection details.

Extension state lives in PostgreSQL, so physical branches inherit it and then
can diverge independently. Restart and suspend/resume preserve it. New engine
bundles preload `pg_stat_statements` on normal computes; older bundles preserve
their `neon`-only configuration. Private export computes retain minimal preload.
The module's SQL views still require enabling it in the database. Disabling the
extension drops those SQL objects; it does not unload the preloaded library or
stop its internal statistics collection. Explicit disable survives restart.
Utility-statement tracking is disabled so private control-role credential DDL
is not retained in statement statistics. This supplies
a prerequisite for [#248](https://github.com/supabricks/platform/issues/248); it does not implement query activity, retained metrics,
or latency percentiles.

## Analytical sync and governed access

Sync checks every stored column against its existing type mapper. Extension
functions, operators, indexes and statistics are not replicated into analytics.
`citext` is unsupported; silently treating it as text would lose its comparison
semantics. UUID output from `uuid-ossp` and bytea output from `pgcrypto` are also
currently unsupported. Using an extension to produce an already-supported text
or numeric column does not itself disqualify that column. Unsupported data is
rejected, never silently coerced. Catalogue extension-owned types are marked
unsupported independently of whether the extension is enabled.

This initial API is for the local operator console. Signed-in governed ingress
cannot invoke operator requests, and even operator ingress refuses governed
branches. The current governed whole-branch profile rejects extension-defined
routines; extending that profile needs separate privilege and ownership
qualification, tracked in [#342](https://github.com/supabricks/platform/issues/342). Neither branch ownership nor this catalogue grants new governed
DDL authority.

## Qualification

`e2e/native/extensions.py` exercises real installed extension functions, required
and unknown-name rejection, dependent-table protection, physical branch
inheritance/isolation, restart and suspend/resume, stale revisions, project
isolation and CLI access. The engine's relocated-bundle smoke separately tests
all five native modules. Both Linux x86_64 and macOS arm64 must pass before the
new engine artifacts become the pinned release inputs.
