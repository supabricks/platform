# A01: frozen Postgres exports

A01 produces an **unpublished** directory of Delta tables from one frozen
Postgres branch. It does not provide analytical SQL, an active epoch, or a
cross-table publication pointer. A02 owns publication and retention; A03 owns
the query surface.

## Developer setup

Start the native runtime using the E01 engine bundle and qualified helpers, and
initialize a project as described in the local runtime guide. From a platform
checkout, install the pinned worker environment once:

```sh
uv sync --project python/analytics --locked --managed-python
supabricks analytics configure \
  --python "$PWD/python/analytics/.venv/bin/python" \
  --worker "$PWD/python/analytics/export.py" --project /path/to/project
supabricks analytics export --branch main --key my-export --project /path/to/project
supabricks analytics status EXPORT_ID --project /path/to/project
supabricks analytics cancel EXPORT_ID --project /path/to/project
```

The daemon stores private absolute worker paths. Keep the checkout and virtual
environment available. This is an engineering setup; bundling the worker into
the curl installer remains release work. No dependency installation happens
while exporting. Export status is distinct from `operation get`: that operation
tracks temporary branch preparation, not the complete export lifecycle.

## Lifecycle and isolation

Admission atomically journals a hidden child branch, a work lease and the export
record. A single export, including cleanup, is admitted per installation.
Idempotency keys are scoped to the project and reject changed parameters.
The existing P04 machinery captures `pg_current_wal_flush_lsn()`, pins that exact
source LSN, waits for storage ingestion and forks the child timeline. The parent
continues handling writes. Transactions not committed at that boundary are
absent from the export, even if committed later on the parent.

The child is hidden from application branch listing, selection, connect and
lifecycle mutations. A generated, nonowner role inherits `pg_read_all_data` and
has read-only sessions by default. Provisioning revokes inherited PUBLIC table
and column privileges on the child; its SELECT access comes from `pg_read_all_data`. Its credential and the input protocol live
in the daemon's private state directory. HBA admits only this exporter and the
compute-control role on the child. Application-owner login is disabled. Compute
settings disable autovacuum, logical replication workers and inherited preload
workers except Neon; inherited session/local preload settings are reset.
This is process and database isolation within one user's installation, not a
sandbox against that operating-system user or a hostile engine extension.

The worker verifies tenant/timeline identity and performs catalog discovery and
all scans inside **one REPEATABLE READ READ ONLY transaction**. Server-side
cursors fetch bounded chunks; a server-side size guard rejects oversized text
before libpq materializes it. Delta tables receive bounded, unpartitioned
batches. Empty source tables get valid empty Delta tables too.

The manifest records the project, source and export branch/timeline IDs, exact
LSN, database OID, transaction snapshot, column mappings, row counts, Delta
versions, per-table scan statistics, and file sizes/SHA-256 hashes. Files and
directories are synced before the worker writes its completion report.
`published` remains false. Success is reported only after the daemon fences the
worker and deletes the temporary compute/timeline, credentials and lease.

Preparing → exporting → cleaning → complete/failed/cancelled is durable in
SQLite. Cancellation and deadlines fence the worker before deleting output.
Daemon recovery fences owned processes and marks interrupted workers failed,
then resumes cleanup. A crash during preparation can resume the already pinned
boundary. A crash during cleanup reuses the same deletion operation. Graceful
shutdown also interrupts an active export; cleanup resumes on the next startup.

## Initial data contract

Only ordinary permanent tables in `postgres` are exported. Views, sequences and
extension-owned relations are listed as omitted. The engine-owned
`public.health_check` and `neon_migration.migration_id` are also explicitly
omitted: compute_ctl mutates this control state after branching. Application
ownership of those reserved names is rejected. Partitioning, inheritance,
foreign/materialized tables, RLS and unsupported column names are rejected.
Limits are 256 discovered relations, 128 exported tables and 128 columns/table.

| Postgres type | Arrow/Delta representation |
| --- | --- |
| boolean | boolean |
| smallint, integer, bigint | signed 16/32/64-bit integer |
| text, varchar | UTF-8 string |
| numeric(p,s), 1 ≤ p ≤ 38, 0 ≤ s ≤ p | exact decimal128(p,s) |
| date | date32 |
| timestamp | microsecond timestamp without timezone |
| timestamptz | UTC microsecond timestamp |
| NULL | nullable value of its declared type |

Unbounded numeric, excess precision/unsupported scale, nonfinite values,
unsupported types (including float, UUID, JSON, arrays and domains), and temporal
values outside the decoder's representable range fail explicitly. No lossy
casts are attempted. Explicit/non-default collations are rejected. Default
collation identity is recorded: string values survive, but PostgreSQL ordering
and equality semantics are **not** reproduced by the analytical engine.

## Resource limits and evidence

Defaults: one active export, 1 GiB output and five minutes total. Admission accepts
16 MiB–16 GiB and 10–1800 seconds. The worker caps rows at 256 KiB, pending batches
at 8 MiB/4096 rows, and fetches 32 rows at a time. A generation may create at
most 1024 Delta batches, bounding retained file-action metadata as well as row
buffers; larger sources fail explicitly even if their byte budget remains. It checks output use and free
space before/after each batch, conservatively reserves encoding/metadata space,
and leaves 64 MiB free. These are application budgets, not filesystem quotas;
other processes can consume disk concurrently. PostgreSQL statements have a
30-second timeout and locks a two-second timeout. The daemon enforces the total
deadline across preparation, scanning and worker startup.

Successful staging directories remain private under
`DATA_DIR/analytics/staging/EXPORT_ID`; they consume disk until discarded or
adopted by future A02 retention. A01 does not advertise these as a published
snapshot. Failed/cancelled generations are removed automatically.

`e2e/native/exports.py` exercises a real Neon PG17 child, exact decimals and nulls,
empty tables, concurrent parent transactions and DDL, unsupported data,
output limits, cancellation and daemon-crash cleanup. Its artifact identifies
bulk scans on the export compute and records parent probe latency, WAL movement and pageserver storage-request/cache
counter deltas across the worker and cleanup interval. Parent probes include
process launch overhead and concurrent SQL writes. Shared storage is still shared: exporting can contend for pageserver,
CPU and disk resources. These synthetic observations are not an isolation or
throughput guarantee.
