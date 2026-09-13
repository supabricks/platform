# Import JSON, JSONL and Parquet

Use the console's **Import file** picker or the existing CLI/MCP ingestion
workflow. Imports create a new PostgreSQL table on the explicitly selected
branch; they never append to or overwrite a table. Your device file is copied
into private staging and remains unchanged. Review the bounded sample and column
mapping, then approve the destination. Success means PostgreSQL committed both
the table and its durable import receipt.

| Input | CLI format | Mapping / limit |
| --- | --- | --- |
| CSV / TSV | `csv` / `tsv` | Existing explicit text/scalar conversions; 100 MiB |
| JSONL / NDJSON | `jsonl` | One object per line; 100 MiB, 1 MiB per row; blank lines rejected |
| JSON array | `json` | Top-level array of objects; 10 MiB, 1 MiB per row |
| JSON document | `json_document` | One complete value into one jsonb column; 10 MiB |
| Parquet | `parquet` | Typed columns with reviewed mappings; 100 MiB source, 512 MiB decoded ceiling |

The CLI recognizes extensions; an explicit `--format` overrides them. A `.json`
file defaults to array mode. In the console choose **JSON — entire document as
jsonb** before selecting a document. Extensionless files require an explicit
format; unsupported types fail rather than guessing another parser.

```sh
supabricks ingest inspect orders.jsonl
# Save inspection.mapping from the response as approved.json, then review it.
supabricks ingest load --source SOURCE_ID --schema-file approved.json \
  --branch main --table orders --key import-orders --wait

supabricks ingest inspect payload.json --format json_document
supabricks ingest inspect orders.parquet
```

MCP exposes the same actions. Use canonical `format` names `csv`, `json_lines`,
`json_array`, `json_document` or `parquet` in `ingest_inspect`, then poll
`ingest_source`. Pass its reviewed mapping to `ingest_load` with an explicit
project/branch revision, source identity, destination and idempotency key.
No notebook or user package installation is necessary; this uses the installed
private worker and works offline.

JSON object mappings use exact top-level key names. Missing keys become SQL NULL;
explicit JSON null remains JSON null in jsonb columns. Nested objects and arrays
are preserved as jsonb. Mixed sampled types also propose jsonb. Keys arriving
after the sample that are absent from the approved mapping reject the entire
import; there is no implicit flattening, skipped record or schema evolution.
Review or expand a mapping explicitly, or normalize the source before retrying.
Empty/oversized keys, duplicate keys, invalid Unicode, NUL, nonfinite numbers and nesting beyond 64
levels are rejected. A required column rejects missing/SQL-null values.

Parquet decimals preserve declared precision/scale up to precision 38. Unsigned
64-bit integers use numeric(20,0), preserving values larger than signed bigint.
The source type panel records timestamp units and timezone labels; timestamptz
stores the instant, not the original timezone name. Nonzero sub-microsecond
precision is rejected. JSON-compatible lists and structs use jsonb; binary, map,
unsupported logical types and nested temporal values are rejected.

**Analytical compatibility:** jsonb is currently PostgreSQL-only. A table with
jsonb columns can prevent a branch's analytical refresh; the previous published
snapshot remains available. Scalar mappings supported by the analytical exporter
can be refreshed and queried through `supabricks analytics` or a notebook. The
separate analytical SQL workspace is still C03 follow-up work.

The preview contains at most 100 rows and 256 KiB; it is not whole-file
validation. Every row is checked while loading. Unsupported values, late type
changes, precision loss or malformed records roll back the new table. Check
`ingest status JOB_ID` after an interrupted command. Retry a failed, retryable
job with `ingest retry JOB_ID --wait`; use the original request key to recover
an uncertain submission. Cancellation may discover an already committed import;
trust the reconciled status. See [CSV ingestion](csv-ingestion.md) for common
staging retention, cancellation, limits and recovery commands.
