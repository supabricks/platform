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
start a Sail reader. Catalog-backed Spark sessions follow in UC04, and browser
Data controls in UC06.
