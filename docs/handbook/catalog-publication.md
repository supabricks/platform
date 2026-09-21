# Publish a snapshot to Unity Catalog

Select a project and use the qualified managed-local catalog. First create a
complete analytical snapshot with the existing `analytics refresh` workflow,
then initialize this deployment's namespace:

```sh
supabricks catalog metadata ensure-namespace --project .
supabricks catalog publication preview EPOCH_UUID --project .
```

Review the tables, snapshot time, destination, locations and durable retention.
Use the returned values exactly:

```sh
supabricks catalog publication publish EPOCH_UUID --project . \
  --key publish-001 --preview PREVIEW_HASH \
  --source-revision SOURCE_REVISION --binding-revision BINDING_REVISION
supabricks catalog publication status PUBLICATION_UUID --project .
supabricks catalog publication resolve --branch main --project .
```

Publish returns a durable journal immediately. Poll status until `published`;
a non-null `error` means work is paused. Retrying the identical publish key returns
the original identity. A changed preview/revision requires a new preview, not
blind retries. For a transient catalog outage, restore service and explicitly
resume the recorded operation:

```sh
supabricks catalog publication resume PUBLICATION_UUID --project .
```

A refresh is another frozen epoch and publication. Previous published revisions
remain retained even after ordinary snapshot leases expire. To retire one, read
the current `binding_revision` from resolve and request:

```sh
supabricks catalog publication unpublish PUBLICATION_UUID --project . \
  --key unpublish-001 --binding-revision BINDING_REVISION
supabricks catalog publication status PUBLICATION_UUID --project .
```

Unpublish blocks new catalog references. It waits for existing readers/consumer
references, removes only the recorded UC metadata, and releases retention after
all objects are gone. It never deletes live PostgreSQL tables. A02 snapshot GC
then applies its ordinary retention policy. The current A02 snapshot remains
protected by its separate current-snapshot reference.

If an alias was recreated, an object moved, or a provider identity changed,
cleanup stops and retains the data. Do not repair this by copying ownership
properties or editing control-state UUIDs. UC07 will provide reviewed recovery
and reconciliation. Until then, preserve the journal and metastore together.

MCP tool `catalog_publication` and console workspace command
`catalog_publication` accept the same nested `command` contract. Projectless
operations are rejected. `resolve` describes a complete publication; it does not
start a Sail reader. Use the catalog read commands below; browser Data controls
remain UC06.

## Read a published revision

```sh
supabricks analytics open --catalog --branch main --wait --project .
supabricks analytics sql --catalog --branch main --sql 'SELECT * FROM public.orders' --project .
supabricks spark shell --catalog --branch main --project .
```

`--catalog` resolves the branch's complete published revision once. Without it,
the existing local snapshot workflow is unchanged. `--catalog --epoch EPOCH_UUID`
selects an older still-published revision from this deployment. There is no
fallback when publication, provider, UUID, schema or location validation fails.

Session metadata includes `catalog.publication_id`, `revision`, namespace UUIDs
and table UUIDs. Read it from session status, notebook epoch metadata or
`SELECT metadata_json FROM _supabricks.epoch`. Tables are available as
`public.orders` (or their original PostgreSQL schema), and as
`<catalog>.<source_schema>.<table>`. The exact physical UC name
`<catalog>.analytics.e_<epoch>_<oid>` is also an alias in that session.
Quote each identifier with backticks when needed.

Publishing another revision changes subsequent opens. Existing sessions and
notebook restarts retain their selected revision. Close/reopen to adopt the new
head. An unavailable catalog blocks new opens but does not interrupt an already
validated reader before its session lifetime expires. Unpublishing withdraws new
admission and waits for existing readers to stop.

API selectors: `analytics_open` accepts `"catalog": true`; console workspace
`analytics`/`open` and `notebook`/`create` accept the same field. These are backend
contracts; the console Data selector is scheduled for UC06. Managed SQL remains
read-only. Spark/Python kernels still run as the trusted local owner, not in a
multiuser security sandbox.
