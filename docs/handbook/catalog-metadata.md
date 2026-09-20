# Project catalog metadata

Open or select a project first. The console transport exposes the metadata API;
the Data browser follows in UC06. From a project checkout:

```bash
supabricks catalog metadata health
supabricks catalog metadata ensure-namespace
supabricks catalog metadata list --branch main --limit 50
supabricks catalog metadata describe ASSET_UUID
supabricks catalog metadata resolve ASSET_UUID --version OBSERVED_VERSION
supabricks catalog metadata validate-source ASSET_UUID --version OBSERVED_VERSION
```

The CLI waits for bounded background work. MCP callers use `catalog_metadata`
with a `command` object, then `poll` the returned request UUID until `complete` or
`failed`. Console clients use the same nested command through the authenticated,
project-selected workspace transport. Project and deployment identities come
from that session, never from command arguments.

Listings distinguish live PostgreSQL tables from published local Delta snapshots.
A snapshot includes its epoch, publication revision and snapshot time. It is not
yet registered as a UC table: UC03 adds that publication step. Resolving metadata
does not authorize or start a data read; UC04 adds the Sail integration.

Keep the asset UUID and `version` from discovery. A schema change or rename
invalidates the old version; inspect the asset and explicitly accept its new
revision. A drop/recreate has a new incarnation and must be rediscovered. Changed
listings invalidate pagination cursors; restart from the first page.

`ensure-namespace` creates a deployment-specific UC catalog and `analytics`
schema. Repeating it verifies the recorded UUIDs. If a name already exists but
has no recorded ownership, or creation was interrupted ambiguously, the command
fails rather than adopting that object. Retain the journal and metadata for
reconciliation; deleting control files is not a repair procedure.

Inspect catalog health separately from an empty table list. Live PG inspection
continues when UC is unavailable, but namespace work requires healthy authenticated
UC. Requests expire on daemon restart or after five minutes; only the latest 32
completed/in-flight requests are retained. Resubmit reads when a request expires.
Namespace ownership remains durable.

See the [UC02 contract](../architecture/uc02-catalog-metadata.md) and
[service handbook](catalog-service.md). Existing data roots need an explicit
backed-up installed upgrade to control schema 14; startup never silently migrates
an existing project.
