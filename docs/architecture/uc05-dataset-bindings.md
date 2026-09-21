# UC05: project dataset bindings

A format-2 project can declare up to eight `resources.dataset` requirements of
kind `catalog_dataset`. Capability `catalog-datasets-v1` distinguishes this
contract. Requirements have a logical label, optional expected schema and content
SHA-256 values, dependencies, and optional source provenance. Provenance is never
used as a destination mapping. Inspect, pack, verify and unpack remain pure source
operations and report unresolved catalog requirements without starting UC.

Destination mappings arrive through `project plan --datasets mappings.json`,
MCP `project_plan` options or the existing console project apply API. Each mapping
supplies the producer deployment, provider and publication UUID. The plan shows
the complete fixed revision, table schemas, snapshot fingerprints and previous
binding. An unmapped new requirement produces an `unresolved` step and cannot be
applied. Existing mappings stay fixed unless explicitly changed. Updates show
schema/content differences; `catalog datasets updates` discovers a current head
without adopting it. A publication UUID from a copied package grants no authority.

The schema fingerprint covers logical table/schema names and ordered column
definitions, sorted by logical schema/table name and excluding physical UC names
and object IDs. The optional content
fingerprint is the exact frozen export manifest SHA-256, not a logical row-set
hash. Matching schema permits a destination-specific publication; matching
content deliberately requires the exact manifest. Plans stay within the existing
48 KiB bound. Large schemas may require smaller publication sets.

Apply journals its dataset retention references atomically with intent. Before
activation, bounded workers validate every remote namespace/table UUID, schema
and canonical local Delta location through the explicit managed-local provider.
No provider request blocks the state writer. Activation rechecks the selected
publications, CAS-updates the deployment revision, replaces dataset mappings,
transfers pending retention to binding references, and publishes the active
revision in one transaction. Failed/cancelled applies release only their pending
references and retain the previous active mappings. Lost responses replay the
original operation.

Bindings live as typed destination resources in the existing deployment journal;
UC03's publication reference table holds their leases. No new catalog schema is
needed. Each installation permits at most 512 binding/pending-apply references;
each project has at most eight datasets. At most two asynchronous apply-validation
workers run. Individual UC calls retain the existing 750 ms/64 KiB limits, with a
30-second aggregate admission budget plus the final bounded publication check.

With `catalog=true`, the analytical session captures the project's installed
bindings once. Each acquires a separate durable publication reference atomically
with session admission. Bound tables appear under
`dataset_<logical-key>.<source-schema>.<table>`; repeated bindings of different
revisions from the same producer get separate private Sail memory catalogs.
Existing local `public.table` names retain their meaning. If the consumer has no
owned catalog publication, its ordinary project-local snapshot is used alongside
the bound inputs (created on first access); no producer table is copied into it.
The consumer therefore uses its own database/branch execution context.

SQL, Spark Connect and notebooks use these same descriptors. Metadata records
each producer deployment, publication, epoch, revision, table UUID, schema/content
fingerprint and snapshot time. `shared_source_transaction=false` makes clear that
joining separately published sets does not imply one source transaction. Live
readers never follow head or binding updates. Opening/restarting a reader is an
explicit new admission using the current fixed mappings; existing notebook
kernels keep their original descriptors until stopped.

Removing dataset declarations produces reviewed `unbind` steps. This is also
the consuming project's dataset decommission path before removing its checkout;
other PK04 resources retain their existing retain-only lifecycle. A filesystem
checkout deletion alone is not a deployment deletion and cannot release durable
references. Unbind affects future readers and never deletes producer tables,
files or UC metadata. Existing reader pins survive binding changes and are
released atomically with terminal session state only after owned kernel/Sail
process death. Daemon recovery uses the same cleanup path.

Producer withdrawal rejects new binding/reader admission and waits for explicit
consumer unbinds and active readers to drain. `catalog datasets list` reports
withdrawn bindings; producer `references` reports counts and bounded holder
identities (binding, apply, session). Binding retention is durable, not silently
expired by wall time; reader lifetimes remain bounded by UC04. Only producer
unpublication retires its owned UC metadata; snapshot GC remains a separate step.

This is the local-owner, same-installation managed-UC profile. It adds neither
remote/cloud storage nor delegated IAM. Browser Data controls and consumer
binding forms remain UC06. Alpha.31 retains local control schema 15; full archive
qualification and existing upgrade/recovery inventory limits remain separate.

## Validation

Local Linux installed-service qualification passed all 37 checks using the pinned
source-built UC server and the qualified Sail/PG baseline. Five UC05 scenarios
cover two consumers without table copies, fixed-revision update and notebook
joins, offline package import without inherited destination authority, reviewed
consumer unbind with live readers, and remote UUID replacement after planning.
The Rust portable suite, analytical worker tests and release-evidence tests also
pass. Fault injection verifies atomic rollback of partially acquired reader pins;
reopening the journal and daemon recovery preserve bindings while releasing
terminated readers. Cross-platform offline CI and complete alpha.31 archive
qualification remain pending; the local run did not isolate the external network.
