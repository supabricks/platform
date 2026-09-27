# Data ingestion and warehouse migration strategy

[Documentation home](../README.md) · [Plan index](README.md) ·
[Stack overview](../stack.md) · [Existing ingestion plan](console-ingestion-implementation.md)

Status: **proposed product and architecture direction**, 2026-09-27.
This document records the Databricks research and the proposed Supabricks loading
experience. It does not declare new connectors, larger import limits, direct
analytical ingestion, or migration compatibility implemented or qualified.

## Product objective

A user should be able to bring existing data into Supabricks, verify what arrived,
and run a useful query without assembling their own transfer pipeline. The first
priority sources are local files, cloud object storage, Snowflake and Databricks.
The same workflow should serve a small evaluation and a bounded bulk migration,
with explicit limits and a recoverable record of progress.

The product promise to develop is: **connect or upload, select data, review its
mapping and destination, load reliably, and start querying**. Ongoing refresh is
an additional capability with a source-specific contract. A successful initial
copy alone does not establish incremental synchronization.

This extends our OLTAP architecture at its entry point. Internal PostgreSQL to
analytics synchronization remains useful for operational data; warehouse data
also needs a path directly into analytical storage.

## What Databricks supports

The following summarizes official documentation reviewed on **2026-09-27**,
primarily for Databricks on AWS. Source availability, release state, privileges,
compute and networking requirements vary. This is a capability comparison, not
a statement that every connector is generally available on every cloud.

| Entry path | Documented capability | Lesson for Supabricks |
| --- | --- | --- |
| Browser upload | CSV, TSV, JSON, Avro, Parquet and text; preview and edit names/types; create or overwrite a managed Delta table. The table-upload flow accepts up to 10 files totaling under 2 GB. [Upload documentation](https://docs.databricks.com/aws/en/ingestion/create-or-modify-table) | Make initial loading accessible, with visible mappings and a clear destination. The competitor's limit is not our proposed capacity guarantee. |
| Cloud files | S3, ADLS and GCS; incremental, idempotent loading with `COPY INTO`, and ongoing file discovery with Auto Loader. Databricks recommends Auto Loader for larger file populations and frequent schema evolution. [Cloud ingestion](https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/) | Support bulk file sets and recurring arrivals through the same owned job framework. |
| Database CDC | Managed connectors include PostgreSQL, MySQL and SQL Server; database pipelines use capture gateways and staging. [Connector concepts](https://docs.databricks.com/aws/en/ingestion/lakeflow-connect) | Initial extraction, change capture and recovery must be specified together before advertising replication. |
| Query-based ingestion | Scheduled reads use a cursor/high-water mark, with current-state, historical and append-only modes. They do not capture every intermediate change like CDC. [Query-based connectors](https://docs.databricks.com/aws/en/ingestion/lakeflow-connect/query-based-overview) | Publish each source's refresh semantics and cursor requirements. |
| Enterprise applications and files | SaaS sources include Salesforce and Workday; enterprise file sources include Google Drive and SharePoint. [Ingestion overview](https://docs.databricks.com/aws/en/ingestion/overview), [file connectors](https://docs.databricks.com/aws/en/ingestion/lakeflow-connect/file-connectors-overview) | A connector framework should support later expansion beyond the first migration sources. |
| Events and direct writes | Message-bus ingestion, including Kafka, and application records through the Zerobus Ingest API. [Ingestion overview](https://docs.databricks.com/aws/en/ingestion/overview) | Event ingestion is a distinct future workload, with its own ordering, replay and admission requirements. |
| Access without copying | Query federation accesses remote databases; catalog federation reads supported external tables through their file storage. Snowflake is a supported federation source. [Federation documentation](https://docs.databricks.com/aws/en/query-federation) | Clearly distinguish remote access from an independent imported copy. |
| Shared data | OpenSharing supports external recipients reading and copying shared data. Version/history and change-feed access depend on provider settings and connector support. [Shared-data access](https://docs.databricks.com/aws/en/opensharing/read-data-open) | Shared-table access is a candidate transfer method, subject to feature and permission checks. |

Databricks also supports partner tools and custom connectors, rather than requiring
every source to have a first-party implementation. Its ingestion documentation
describes integration with catalog governance, orchestration and monitoring.
[Ingestion overview](https://docs.databricks.com/aws/en/ingestion/overview)

Our product conclusion is to build a coherent connection-to-query experience,
with a common execution and recovery model, before expanding connector count.

## Supabricks today

The delivered [file ingestion contract](../handbook/file-ingestion.md) provides
private staging, bounded inspection, approved mappings, durable jobs, cancellation,
retry and PostgreSQL commit receipts across console, CLI and MCP.

| Concern | Current delivered boundary | Proposed extension |
| --- | --- | --- |
| Inputs | CSV/TSV, JSONL, JSON arrays/documents and Parquet | Multiple files, folders and remote source adapters |
| Destination | A new PostgreSQL table on an explicitly selected branch | Independently owned analytical datasets as well as PostgreSQL tables |
| Size | Most source files capped at 100 MiB; JSON arrays/documents at 10 MiB; Parquet has a 512 MiB decoded ceiling | Separately qualified limits, chunked transfer and bounded processing; no arbitrary quota increase |
| Existing tables | Imports do not append to or overwrite existing tables | Explicit create/replace contracts first; append/upsert as later modes |
| Type fidelity | Reviewed mappings, exact supported decimals, full-load validation and rollback on unsupported values | A connector/destination compatibility report with explicit conversions |
| Nested data | JSONB can be imported into PostgreSQL but is unsupported by the analytical exporter and can block refresh | A separately designed analytical type contract; importing into PostgreSQL does not establish analytical compatibility |
| External systems | No delivered Snowflake or Databricks migration workflow | Authenticated discovery, extraction, validation and publication |

The [L01 follow-on](console-ingestion-implementation.md#l01--direct-analytical-dataset-design-following-r04)
already identifies analytical dataset identity, ownership, publication, pinning,
retention and backup as prerequisite design work. This strategy makes that work a
foundation for the proposed loading product; it does not retroactively change
the scope of the delivered local preview.

## Jobs to be done and user flows

| User job | Proposed flow | Successful outcome |
| --- | --- | --- |
| Try Supabricks with my own files | Add data → upload → preview → review mapping → choose supported destination → load → query | The verified dataset remains available after the original file is removed. |
| Bring over warehouse tables | Connect → browse catalogs/databases and schemas → select tables → review compatibility and transfer plan → import → validate | A receipt identifies the source boundary, imported objects, transformations and verification outcome. |
| Load a cloud folder | Connect storage → choose prefix and file rules → inspect file set → select load mode → import | Progress and retries operate on an identified file set without accidental duplicate publication. |
| Recover an interrupted transfer | Open load history → inspect failure → resume or retry eligible work | Completed durable work is reused where valid; uncertain destination commits are reconciled. |
| Keep imported data current | Select an imported dataset → configure a supported refresh mode → review cadence and change semantics | Last successful refresh, source boundary, failures and observed freshness are visible. |

The console should have one **Add data** entry with source choices for upload,
cloud storage, Snowflake and Databricks. A shared flow then covers connection,
discovery, selection, preview, mapping, destination, execution and validation.
Existing [console jobs](../../console/docs/jobs-to-be-done.md) and
[user flows](../../console/docs/user-flows.md) are the UI design starting point;
frontend implementation remains owned by the console repository.

Destination labels should describe the user's purpose: an operational PostgreSQL
table or an analytical dataset for SQL/notebooks. Do not expose an analytical
destination selector until its ownership and publication semantics exist.

Progress should distinguish bytes transferred, rows parsed, rows staged and rows
committed/published. Show exact totals only when known; otherwise report counts
and estimates honestly. Completion should lead directly to querying the selected
dataset, with the import receipt still accessible.

## Proposed architecture

```text
Local files    Cloud object storage    Snowflake    Databricks
     |                 |                  |             |
     +-----------------+------------------+-------------+
                              |
                    Source-specific adapters
            Connect / discover / inspect / extract
                              |
                Shared ingestion job framework
          Reviewed plan / owned staging / checkpoints
          Resource limits / retry / cancel / validation
                              |
                  +-----------+-----------+
                  |                       |
          PostgreSQL destination   Analytical destination
          Transactional load       Delta / Parquet generation
          + commit receipt         + durable publication receipt
                  |                       |
          Operational queries      Catalog + pinned Sail sessions
                  |                       |
          Existing PG-to-analytics sync    Notebooks / analytical SQL
```

Reuse the existing Rust control plane, durable operation model, staging/parser
contracts, catalog integration and session pinning where their invariants apply.
PostgreSQL and analytical destinations need separate commit/reconciliation
adapters beneath that common job model. New analytical ownership will require
changes to today's branch-oriented publication model; it is not just a new
parser or destination flag.

The proposed analytical destination is a project-owned dataset with a stable ID
and immutable published revisions. It must remain meaningful without a source
PostgreSQL table. The L01 design must settle branch associations, cross-dataset
publication groups and dataset bindings explicitly. An imported warehouse must
not silently acquire PostgreSQL branching or cross-table transaction guarantees.

Every connector should declare supported discovery, snapshot, filtering,
partitioning, resume, type mapping and refresh capabilities. The shared framework
must not assume a Spark connector can run unchanged in Sail merely because both
offer Spark-compatible interfaces. Qualify each adapter against our installed
Python/Arrow/Sail stack and record any additional dependencies.

## Initial source strategy

### Uploads

Extend the existing flow with multiple-file selection, explicit combination rules
and resumable transfers. Define whether a file set forms one table or several;
show incompatible schemas before execution when detectable. Inspection remains
a sample, and every loaded row still requires validation.

Keep file identity, checksums and completed-chunk receipts so resuming does not
silently substitute a modified file. File-count, decoded-size, disk and memory
limits remain enforced. Compressed inputs and new formats require separate
parser and resource qualification. Existing upload limits remain until replaced
by measured, documented envelopes.

### Cloud object storage

Start with S3-compatible storage, then qualify Azure and GCS adapters separately.
Allow a bucket/prefix, file filters and either a frozen initial file set or a
later supported recurring mode. Record object version identities where available;
an ETag is not universally a content checksum. Detect changed objects and define
overwrite/deletion behavior rather than treating a path as immutable forever.

Keep credentials behind governed connection objects. Shared credentials or broad
bucket access must not become implicit permission to publish every object.

### Snowflake

Provide database/schema/table discovery, column selection and an explicit
destination mapping. Evaluate query extraction for bounded transfers and staged
bulk export for larger transfers. Snowflake supports `COPY INTO <location>` for
tables or query results, partitioned file output and internal/external stages;
Parquet is one supported unload format.
[Unloading workflow](https://docs.snowflake.com/en/user-guide/data-unload-overview),
[unload formats](https://docs.snowflake.com/en/user-guide/data-unload-prepare)

The transfer plan must identify required privileges, the chosen extraction method,
source snapshot boundary, stage ownership and cleanup. Check decimals, timestamp
semantics, case-sensitive identifiers and semi-structured types before promising
fidelity. Surface source warehouse and data-transfer implications; show unknown
costs as unknown. Never silently change a failed bulk transfer into a different
extraction mode with different consistency or cost.

Start with verified snapshot imports. A future refresh adapter must explicitly
handle keys, updates, deletes, late commits and schema changes. Cursor polling,
Snowflake-specific change tracking and full replacement are different contracts.

### Databricks

Provide workspace authentication and catalog/schema/table discovery. Evaluate
SQL extraction, provider-authorized sharing and supported bulk exports as distinct
transfer methods. The SQL Connector for Python supports remote SQL and batched
Arrow fetching. OpenSharing offers external shared-table access, with historical
versions and change feeds conditional on provider configuration and client support.
[SQL connector](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector),
[shared-data access](https://docs.databricks.com/aws/en/opensharing/read-data-open)

Prefer an explicit table version or equivalent source boundary where supported.
Check table protocol features, column mapping, deletion vectors, nested types and
client compatibility. Copying arbitrary Parquet files from a Delta directory is
not a correct table import: the selected table state must be resolved by a
compatible reader. A shared table available remotely is not yet an independent
local copy. Imported data needs its own ownership, publication and validation.

Data migration does not automatically migrate notebooks, jobs, views, functions,
permissions or SQL dialect behavior. Discovery should report these objects and
unsupported features clearly; migrating them is separate work.

## Shared correctness and operating contract

Each admitted job binds a stable source identity, authorized actor, destination,
reviewed mapping, extraction boundary and idempotency key to a durable plan.
Separate reusable connection configuration from per-run credentials and secrets.
Console, CLI and MCP must expose the same backend admission and status semantics.

Checkpoint completed transfer work only after its staging data and receipt meet
the durability contract. Publish destination completion atomically with the
corresponding authoritative receipt. After a crash or uncertain response, inspect
the destination's committed state before retrying; transfer checkpoints alone
do not prove successful publication. Cancellation can race with completion and
must reconcile the actual outcome.

Preserve the previous analytical revision until the new revision is complete and
durable. Define whether a multi-table import publishes independently per table or
as an admitted group. Record the consistency actually available from the source;
multiple successful table reads do not establish a shared source snapshot.
Existing reader pins must survive replacement and govern reclamation.

Mappings must preserve supported decimals, integer ranges, null semantics and
timestamps. Never silently truncate values or substitute NULL for failed casts.
Default to a failed load for incompatible data. Any later quarantine/partial-load
mode needs explicit user selection, visible rejected-row counts and its own
completion semantics. A preview is not a validation certificate.

Reuse identity, authorization, audit and governed execution boundaries. Remote
connectors need explicitly admitted outbound access and scoped credentials;
neither notebook code nor imported content should inherit broad control-plane
secrets. Source permissions do not automatically become destination grants.
The local-owner profile retains its existing trusted-owner boundary.

Retention must cover staged chunks, failed attempts, source manifests, published
revisions and live reader references. Backup/restore must distinguish owned
dataset bytes from renewable remote access, and require explicit reconciliation
before restored jobs resume source extraction. Cleanup must remove only owned
artifacts, including any remote staging objects created for a transfer.

## Proposed delivery sequence

These are proposed phases, not assigned implementation slice IDs or completion
claims. A detailed implementation plan should define migrations, budgets and
acceptance fixtures before each runtime change.

| Phase | Scope | Exit evidence |
| --- | --- | --- |
| 1. Destination and job contracts | Resolve L01 identity/publication semantics; inventory reusable ingestion code; specify adapter, receipt and recovery interfaces | Reviewed state transitions and a bounded direct-analytical landing spike with real Sail reads |
| 2. Analytical landing | Managed analytical dataset creation, publication, catalog integration, pins, retention and recovery | Data remains queryable without the original file; failed publication preserves the prior revision; installed backup/restore passes |
| 3. Upload product | Multiple files, resumable staging, clear validation and eligible destination selection | Browser reload, transfer interruption, stale-file detection and malformed late records have tested outcomes |
| 4. Cloud-file ingestion | S3-compatible connection/discovery and bounded initial bulk loads | Replay-safe identified file sets; changed/deleted objects, expired credentials and retries are exercised |
| 5. Warehouse snapshot imports | Databricks and Snowflake adapters as separately measured slices | Real source fixtures, reviewed type mapping, extraction-boundary receipts, content validation and recovered interruptions |
| 6. Refresh modes | Add supported scheduled replacement, incremental query or change-feed modes one source/mode at a time | Update/delete/late-arrival/schema-change semantics and checkpoint expiry are demonstrated |

Qualification is required for every shipped phase, not postponed until phase 6.
Reusable connection foundations should precede source-specific implementations.
Broader SaaS, additional database CDC, event streams and federation can follow
when their demand and operating contracts justify them.

## Measurement and acceptance

Measure improvement after every logical slice using predeclared workloads and
fresh comparable runs. Separate upload/network, extraction, parsing/conversion,
staging, destination write, publication and verification time. Retain failures,
rejected attempts and host-contention evidence. Do not run these experiments
alongside the deferred synchronization benchmarks or other competing builds.

The workload matrix should vary bytes, rows, file counts, row width, supported
types, destination and concurrency. Establish safe resource budgets before
choosing larger sizes. Report end-to-end time to first successful query, sustained
bytes/rows per second, peak memory/disk, source load and restart cost. No TB-scale
or concurrent-user claim follows from a small single-job success.

Acceptance must include:

- Exact values against deterministic fixtures, row counts and suitable partition
  checksums at the recorded source boundary; row counts alone are insufficient.
- Loss/duplication checks across network interruptions, credential expiry, process
  death, uncertain commit responses, cancellation, disk pressure and restart.
- Type/schema fidelity, unsupported-feature refusal and unchanged source data.
- Tenant/project authorization, scoped connection use and admitted worker access
  in the applicable governed profile.
- Real console/CLI/MCP workflows and exact installed-package tests for each
  advertised OS/profile. External-source qualification requires controlled
  networked fixtures; it does not inherit offline-package evidence automatically.

The existing PostgreSQL-to-analytics performance workstream continues to own its
throughput and publication-lag targets. Bulk ingestion has separate workloads and
denominators. Shared runtime changes still need the affected synchronization
regression and qualification checks; faster extraction alone is not an end-to-end
ingestion improvement.

## Decisions to resolve before implementation

1. Final analytical dataset/branch association and multi-table publication model.
2. First supported type matrix, formats, file-set rules and operating envelopes.
3. Connection secret storage, outbound-network admission and installed connector
   dependency/build contracts for local and governed profiles.
4. Source snapshot guarantees and selection criteria for SQL, sharing and staged
   export; deterministic fallback/refusal behavior.
5. Which create/replace semantics ship first, and which refresh modes justify
   later work. Snapshot import must remain useful on its own.
6. Migration validation depth and when source compute or transfer estimates can
   be provided reliably.

The recommended initial scope is uploads, S3-compatible storage, Snowflake and
Databricks snapshot imports, backed by direct analytical landing and a shared
durable job framework. It is a proposal for a new workstream, not an extension
of the current SP06 benchmark task or an assertion of connector parity.
