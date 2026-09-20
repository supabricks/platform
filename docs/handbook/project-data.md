# Move selected PostgreSQL tables

Use a `.sbdata` file to move a small, supported table set between local Supabricks
deployments. It is a companion to a source `.sbproj` package. Importing source or
opening a notebook does not load data automatically.

In the source project's directory, select tables explicitly:

```sh
cat > tables.json <<'JSON'
{"version":1,"tables":[{"schema":"public","name":"sales"}]}
JSON
supabricks project data export --branch main --tables tables.json --output sales.sbdata
supabricks project data inspect sales.sbdata
```

The output path must be new. Export reads all selected tables from one consistent
snapshot. The file includes schema and real table data; transfer it deliberately.
Inspection reports hashes, types, constraints and row counts without printing rows.
Inspection works offline and never starts a runtime.

On the destination, first unpack/create or attach the intended project through
the normal project workflow. Then select a fresh database for the import:

```sh
supabricks up
supabricks database create imported --key imported-db --wait
supabricks project data verify sales.sbdata
supabricks project data import sales.sbdata --branch imported --key sales-v1
supabricks sql --branch imported --sql 'SELECT count(*) FROM public.sales'
```

Every imported table name must be new. The command never overwrites or appends
to existing tables. All tables and the receipt commit together; the destination
gets its own runtime IDs, database credentials and table ownership. You can then
use normal SQL, analytics and notebook workflows for types supported by those
engines. PG data portability does not expand Sail's analytical type support.

If the command is interrupted or its reply is lost:

```sh
supabricks project data status --branch imported --key sales-v1
supabricks project data import sales.sbdata --branch imported --key sales-v1
```

The same key and exact file reconcile the existing receipt. A different file
under that key fails. `not_committed` may mean another attempt is still running;
it does not authorize deleting destination data. Use Ctrl-C to interrupt the
foreground command. No platform job continues after its client connection closes.

Version 1 supports PG17 UTF-8 tables with compatible database locale/version,
common scalar types (including numeric, UUID, JSON/JSONB and bytea), NOT NULL,
primary keys and ordinary UNIQUE constraints. It rejects sequences/serial/identity,
defaults, foreign keys, CHECK constraints, standalone/custom indexes, extensions,
arrays/domains, partitioning, RLS and triggers. Unsupported declarations fail
before archive publication; failed imports roll back the whole table set.

Limits: 16 tables, 64 columns/table, 32 MiB decoded COPY data, 66 MiB file,
100,000 total rows, one million cells and 256 KiB per row. Operations have a
five-minute deadline. Roles, grants, functions, Delta epochs and physical storage
are outside this profile. Use stopped cell backups for physical recovery.
