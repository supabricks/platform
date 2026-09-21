# Share a published dataset with another project

Publish a complete snapshot in the producer project using the
[catalog publication workflow](catalog-publication.md). Record its deployment,
provider and publication IDs. In the consumer's format-2 `supabricks.toml`, add:

```toml
[resources.dataset.sales]
kind = "catalog_dataset"
requirement = "sales.orders.v1"
# Optional constraints returned by catalog datasets describe:
# expected_schema_sha256 = "..."
# expected_content_sha256 = "..."
```

Keep a consumer database declaration (normally `resources.database.main`) for
its SQL/notebook execution context. Put destination choices in a separate JSON
file, outside the package's include patterns:

```json
{
  "dataset.sales": {
    "deployment_id": "PRODUCER_DEPLOYMENT_UUID",
    "provider_id": "PROVIDER_UUID",
    "publication_id": "PUBLICATION_UUID"
  }
}
```

Inspect and review before applying:

```sh
supabricks catalog datasets describe PUBLICATION_UUID --project ./consumer \
  --owner PRODUCER_DEPLOYMENT_UUID --provider PROVIDER_UUID
supabricks project plan --project ./consumer --datasets ./mappings.json > plan.json
supabricks project apply plan.json --project ./consumer --key bind-sales
supabricks project status APPLY_UUID --project ./consumer
supabricks catalog datasets list --project ./consumer
```

Poll apply until `succeeded`. No source IDs in package provenance are implicitly
adopted. Missing mappings show `unresolved` steps and block apply. Apply failures
leave the previous active mappings in place.

```sh
supabricks analytics sql --project ./consumer --catalog --branch main \
  --sql 'SELECT * FROM dataset_sales.public.orders'
supabricks spark shell --project ./consumer --catalog --branch main
```

Notebook creation and console analytics opens accept `"catalog": true`. Browser
Data controls arrive in UC06. Each declared dataset gets a `dataset_<name>` SQL
catalog; quote each identifier with backticks for names containing hyphens.
`public.table` continues to mean the consumer's local snapshot. Different inputs
retain separate revisions and snapshot times, visible in
`SELECT metadata_json FROM _supabricks.epoch` and notebook/session metadata.

To discover an update without changing anything:

```sh
supabricks catalog datasets updates dataset.sales --project ./consumer
```

Choose the new publication in the mapping file, revise any expected fingerprint,
then plan and apply again. Review the old/new schemas and provenance in the plan.
Only newly admitted readers (including explicitly restarted notebooks) use the
updated mappings. Running readers keep their earlier revisions.

Source packages carry these logical requirements and optional constraints.
`project pack`, `verify` and `unpack` work without UC. An imported package on
another deployment requires fresh destination mappings, even if its source
provenance lists valid local UUIDs.

To remove access or decommission the consumer's dataset usage, remove its dataset
declarations, review the resulting `unbind` steps and apply them before deleting
the checkout. This releases binding retention without deleting producer data;
already-running readers retain their leases until stopped. Merely deleting a
checkout does not remove its private deployment state.

A producer can inspect what holds a withdrawn publication:

```sh
supabricks catalog datasets references PUBLICATION_UUID --project ./producer \
  --owner PRODUCER_DEPLOYMENT_UUID --provider PROVIDER_UUID
```

Withdrawn bindings reject new readers. Unbind the listed consumer mappings and
close remaining readers to let producer unpublication finish. Existing snapshots
remain subject to ordinary snapshot GC after catalog retention is released.
