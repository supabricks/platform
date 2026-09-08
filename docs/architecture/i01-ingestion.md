# I01: owned CSV ingestion

I01 implements the I00 source/job/receipt contracts without a catalog migration.
The local catalog remains version 9; the native preview becomes alpha.7. The
worker and its hash are in the release inventory and use the existing locked
analytical Python runtime. PostgreSQL-only archives do not include ingestion.

The daemon owns SQLite and asynchronous worker supervision. Its launch gate
records native process ownership and the job's loading attempt before Python
runs. Stage workers copy a regular device file to a private source slot, check
its identity before/after copying, hash and inspect the private copy. After
fencing the worker, the daemon publishes immutable source metadata. The load
worker rehashes the staged source before opening PostgreSQL.

Arrow parses bounded batches as strings. Conversion follows an explicit approved
mapping, with positional inputs, typed scalar allowlists and no expressions.
Psycopg quotes target identifiers and streams rows through COPY. The target table
and canonical `_supabricks.ingest_receipts` row share one transaction. Receipt
schema and primary-key shape are validated; inherited receipts do not match a
new branch origin.

The loader and every receipt check take the same transaction-scoped advisory
lock derived from installation origin, project, branch and job. Receipt reads
use READ COMMITTED after acquiring that lock. This prevents a fenced worker's
still-running PostgreSQL backend from committing after a checker incorrectly
reports absence. Network, schema or receipt conflicts remain unresolved. A
pre-existing destination detected before any target DDL/COPY is a terminal,
nonretryable rejection; it does not represent an ambiguous attempted commit.

Workers bootstrap their private connection using the daemon's control credential
and immediately SET ROLE to the ordinary database owner. Source and target SQL
runs under that role. Session-local logging settings suppress raw COPY error
context. Owned control sessions remain accessible during TTL's application-role
drain; the durable active job blocks deletion until the origin receipt resolves.
New imports still require an unexpired, converged running branch.

Each service tick reports parsed/copied progress, observes worker completion,
fences the process group and reconciles receipts. Cancel/shutdown uses the same
path. Unknown outcomes hold lifecycle protections. Restart fences all recorded
workers, deletes temporary credentials/previews, and resumes receipt checks
without replaying COPY. No byte-offset resume exists.

The release qualification harness uses fresh private roots and synthetic data.
It drives the real CLI, daemon, PostgreSQL, analytical exporter and MCP, including
worker/daemon death around commit. It records exact 100 MiB source throughput,
decoded bytes and sampled peak RSS. Signed localhost installers qualify the same
archive on Linux x86_64 and macOS arm64 with external networking denied. Parser
unit tests also inject ENOSPC and source mutation, and exercise decoded/deadline
limits. These are process-failure tests, not power-loss qualification. See the
[operator workflow](../handbook/csv-ingestion.md) for limits and recovery behavior.
