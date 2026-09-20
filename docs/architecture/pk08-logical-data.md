# PK08 — PostgreSQL logical data packages

Status: implemented; native and exact-archive qualification required before
release completion. Candidate alpha.25, catalog schema 13. The PG receipt table
is versioned independently; no SQLite migration is needed.

## Decision and boundary

Ship an explicit `.sbdata` companion to the source-only `.sbproj`. It transfers
a selected table set, including typed schema declarations and PostgreSQL text
COPY data. Source packaging, project apply and notebook opening never import it
implicitly. The destination is an explicitly selected, already bound database;
every imported table name must be fresh. Existing unselected tables may remain.
No runtime project binding or active project revision is created or changed.
The new table bindings and import receipt become visible together at PG COMMIT.

This is the first logical data adapter, not arbitrary database backup or a
replacement for stopped cell recovery. A project source archive and its data
companion are independently verified artifacts; exporting them is not a joint
source-code/database transaction. The data archive records the inspected source
definition digest, and export refuses an observed source change before publication.

The CLI uses the existing binding resolver and authenticated loopback gateway.
There is no new public network endpoint, remote database connection option,
browser upload flow or shared-user authorization model. The actor is the current
local owner. Source selected tables must belong to the database application owner;
new tables belong to the destination application owner. Future governed export
rights remain an IAM workstream, not authority granted by archive provenance.

## Adapter comparison

| Path | Evidence and tradeoff | PK08 decision |
| --- | --- | --- |
| `pg_dump` / `pg_restore` | PG17 schema probe preserves serial sequences and defaults. PostgreSQL documents consistent dumps and cross-architecture logical restoration, but restores execute source-controlled SQL. Selected tables do not automatically include all dependencies. | Suitable for a separately reviewed trusted-dump adapter; not this inspectable typed profile. |
| Existing frozen export / Delta | A01 captures a frozen database and publishes leased epochs; its Arrow mapping intentionally supports a narrower PG type set and omits relational schema semantics. `bytea`, UUID, JSON and unconstrained numeric do not fit that mapping. | Keep it for analytics; do not represent an analytical snapshot as a lossless database transfer. |
| PG text COPY + typed declarations | Actual PG17 tests cover binary/NULL/Unicode, numeric precision, special float/numeric values, timestamps, supported constraints, atomicity and consistent multi-table reads. Identifiers are quoted; schema SQL is generated only from validated enums. | Selected implementation. No executable SQL, connection URI or physical storage object appears in the archive. |

Sources: [PG17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[COPY text and binary portability](https://www.postgresql.org/docs/17/sql-copy.html),
[database locale metadata](https://www.postgresql.org/docs/17/catalog-pg-database.html).
The frozen-path comparison also follows `python/analytics/export.py`'s current
`arrow_type` and `discover` contracts.

## Format and fidelity

Version 1 is bounded UTF-8 JSON with `content` and `content_sha256`. Content has
profile `postgres_tables`, PG major 17, locale metadata, source provenance and a
table array. Each table declares its schema/name, ordered typed columns,
supported constraints, row count, COPY text and its SHA-256. The content digest
covers Rust's canonical struct serialization; the reported archive digest covers
the exact received file. Digests establish integrity, not a publisher signature.
Inspect/verify reports omit COPY values. Unknown fields and unsupported enum
variants fail rather than becoming SQL fragments. Offline inspection needs no
HOME, daemon or database tools and refuses a symlink as the archive file.
Verification checks structure, integrity and limits. PostgreSQL validates actual
typed field values and relational constraints inside the import transaction;
an integrity-valid artifact can still fail that validation and roll back.
The structural contracts are [archive v1](../../schemas/project-data-v1.schema.json)
and [selection v1](../../schemas/project-data-selection-v1.schema.json).

| Feature | Version 1 behavior |
| --- | --- |
| bool, int2/int4/int8, float4/float8 | Preserved, including supported PG special values |
| text, varchar | Preserved; varchar length retained |
| numeric | Unconstrained or declared precision/scale, including PG17 negative scale |
| date, timestamp, timestamptz | Preserved with ISO output and UTC; explicit timestamp precision modifiers rejected |
| UUID, json, jsonb, bytea | Preserved through PG text representations; binary uses hex |
| NULL, whitespace, embedded tab/newline, literal backslash | Distinguished and escaped by COPY; never treated as executable SQL |
| NOT NULL, primary key, ordinary UNIQUE | Preserved, including column order and constraint name |
| Other indexes, FK, CHECK, deferred constraints, NULLS NOT DISTINCT | Rejected; not silently omitted |
| Defaults, serial/identity sequences, generated/dropped columns | Rejected |
| Views, foreign/unlogged/partitioned/inherited tables, RLS, triggers, rules | Rejected |
| Extension-owned tables, extension/custom/domain/array types, custom access methods | Rejected |
| Explicit column collations, custom index semantics/storage options | Rejected |
| Default database locale | UTF-8 only; builtin PG17 or libc C/POSIX; source/destination locale and actual version must match |
| Delta/Parquet epochs and versions | Not accepted as inputs; no Delta files, snapshot IDs or storage URIs are copied |
| Roles, grants, functions, ownership statements, physical cell state | Not included |

Constraints or objects in unselected relations are not transferred. This is a
selected-table profile; unsupported objects elsewhere in the database do not
prevent exporting an independently supported selection.

## Consistency, ownership and recovery

Export acquires ACCESS SHARE locks on all selected relations, then reads schema
and data in one REPEATABLE READ READ ONLY transaction. Concurrent DML continues;
all tables see the same MVCC snapshot. PostgreSQL retains the live snapshot until
the transaction ends. There are no source Delta epochs to pin or files exposed to
Delta GC in this adapter. Any later epoch adapter must acquire and renew platform
leases before reading its inputs.

Import verifies the whole bounded archive before connecting. It validates PG
major and locale, serializes PK08 imports with a transaction advisory lock,
refuses existing table names, creates schemas/tables, performs COPY and checks
row counts. All DDL/data and `_supabricks.logical_import_receipts_v1` commit in
one transaction. No source database OID, branch UUID or credential is adopted.
The receipt records destination IDs and newly allocated table OIDs separately
from immutable source provenance.

A failed statement or pre-COMMIT kill rolls back the complete set. After an
uncertain COMMIT reply, `project data status` checks the receipt; repeating the
same import key and exact archive returns that receipt without loading again.
Changing archive bytes under the key is a conflict. Removing/replacing an
imported table is also a replay conflict. A receipt describes the completed
import, not a promise that users have never edited the imported rows afterward.
`not_committed` is not proof that another importer has finished: concurrent
attempts still serialize on the PG lock. Ctrl-C closes the client transaction;
there is no detached daemon job to cancel. Destination force-delete/suspend may
interrupt a transfer, which uses the same receipt rules on retry.

## Bounds and qualification

- 16 selected tables, 64 columns/table, 64 constraints/table.
- 32 MiB aggregate decoded COPY data, 66 MiB archive, 256 KiB COPY row.
- 100,000 aggregate rows and 1,000,000 cells.
- 300-second whole-operation deadline; PG statements 30 seconds, lock wait one second.
- Memory is bounded per foreground CLI process; this is not a cell-wide admission
  budget or a large-database capacity claim.

The native-cell suite exercises fidelity, concurrent commits, new deployment IDs,
unsupported schema, data/row limits, late-table rollback and SIGKILL immediately
before/after commit. The R04 portability producer exports one `.sbdata` on Linux;
both exact installed native consumers verify and import those same bytes, prove
binary/decimal/NULL fidelity and retry behavior, and bind the result to archive
and source provenance. All existing release gates remain mandatory. The installed
walkthrough is `PROJECT-DATA.md`.
