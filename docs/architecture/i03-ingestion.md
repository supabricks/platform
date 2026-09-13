# I03: JSON and Parquet in the existing ingestion workflow

I03 extends I01/I02, on top of NE06 merged in platform PR #41
(`main@9c0d5e37d1a7d896749a2061f1fa0139596858a2`). The console source is owned by
`supabricks/console`; platform pins that repository and assembles its assets.
This slice retains catalog 10, ingestion protocol 1, immutable staging, approved
mappings, owned workers, transactional COPY/receipt reconciliation and explicit
retry. No new parser dependency, execution environment or second upload service
is introduced. Release defaults advance to alpha.14.

## Reconciliation with the intervening notebook work

| Existing capability | I03 reuse / remaining boundary |
| --- | --- |
| I00-I02 sources, jobs and browser uploads | Extend format discovery and parsing; same quotas, preview binding, receipts, cancellation and recovery |
| N00-N06 browser notebooks and Spark sessions | Retain all installed regression gates; do not add analytical SQL tabs here |
| NE01-NE06 environments and recovery evidence | Ingestion stays in the private analytical worker runtime, independent of notebook packages; retain final archive evidence gate |
| Separate console repository | Frontend source, browser fixtures and tests live there; platform owns backend, workers and native integration |
| C03 | Still open: analytical SQL workspace, refresh/freshness controls and the complete snapshot demo |
| R04 | Still open: combined format + analytical workspace release acceptance, operating matrix and walkthrough; reuse current exact-archive gates |
| L01 | Still open: direct analytical dataset ownership/publication; all I03 imports create PostgreSQL tables |

## Format and mapping contract

CLI inspection infers CSV/TSV, `.jsonl`/`.ndjson`, `.json` arrays and `.parquet`
from the extension; `--format` overrides it. Document mode is explicit.
MCP uses the existing `ingest_inspect` action with an optional canonical `format`
(default CSV for backwards compatibility). The browser offers filename inference
or an explicit format before upload. Format changes invalidate approval; browser
load admission compares format and parser options against the inspected source.

The existing mapping enum already reserved `json_lines`, `json_array`,
`json_document`, `parquet` and `jsonb`. CSV/Parquet inputs are zero-based column
indices. JSON object inputs are exact top-level keys (no dotted/path expression
interpretation); document input is `$`. Inputs cannot repeat. Every Parquet/CSV
input must appear once. A JSON key encountered outside the approved mapping
rejects the load, including after a valid preview. Missing keys map to SQL NULL
and therefore fail a nonnullable target mapping.

JSON previews propose scalar types for consistent sampled scalar values and
`jsonb` for nested, mixed or otherwise unrepresentable values. JSON null in a
jsonb mapping remains JSON null; an absent key remains SQL NULL. String/number/
boolean changes do not silently coerce across approved scalar types. Document
mode maps exactly one complete JSON value into one jsonb column. Duplicate keys,
nonfinite numbers, NUL, invalid Unicode and excessive nesting are rejected.
Exact JSON numbers never pass through binary floating point for decimal/jsonb
mappings. A double mapping explicitly requests floating point representation.

Parquet uses the existing pinned PyArrow reader in bounded batches. Signed
integers widen to supported PostgreSQL types; uint64 proposes numeric(20,0).
Decimals retain precision/scale (up to 38, nonnegative scale). Timestamp timezone
metadata is shown in inspection; timestamptz preserves the instant, not the
original timezone label. Nanosecond values must be exactly representable at
microsecond precision. Lists and structs containing JSON-compatible values map
to jsonb without flattening. Binary, maps, unsupported logical/extension types,
nested temporal values and out-of-contract decimal types fail explicitly.
Parquet nulls become SQL NULL, including for nested/jsonb columns.

CSV parser options retain their existing meaning. Other formats use the canonical
mapping fields `delimiter=","`, `header=true`, `null_strings=[]`; noncanonical
CSV options are rejected instead of silently affecting JSON or Parquet.

## Bounds and analytical compatibility

The existing 100 MiB source, 512 MiB decoded, sampled 512 MiB worker RSS, disk
reserve, single active load, ten-minute deadline and bounded preview remain.
JSON arrays/documents additionally cap source bytes at 10 MiB. JSONL reads one
bounded line at a time; blank lines are invalid records. Rows cap at 1 MiB, except
a whole JSON document may occupy 10 MiB. Nesting caps at 64, columns at 256.
Parquet footer/Thrift limits and declared uncompressed row-group sizes are checked
before reading batches; the existing watchdog also bounds parser allocations.
This is sampled local resource enforcement, not OS sandboxing or hard quotas.

The Arrow/Sail export type allowlist is unchanged. Scalar fixtures from JSONL,
JSON arrays and Parquet traverse real PostgreSQL COPY, publication and Sail SQL.
jsonb imports work in PostgreSQL but are unsupported by the analytical exporter;
the console explicitly warns that such a table can prevent a branch refresh.
A failed refresh must preserve the previously published epoch. I03 does not
silently omit a table, flatten nested data, or claim JSON analytics support.

## Gateway correction discovered during fault qualification

The gateway previously built its compute-identity map from every owned process
with a branch binding. An ingestion worker also has that binding and could
replace the compute's identity in the map, disconnecting existing PostgreSQL
clients. Gateway invalidation now considers only compute processes. The real
per-format fault tests retain a connection through the public branch gateway
while admitting and stopping ingestion workers; its transaction must survive.

## Evidence

`python/ingest/test_formats.py` checks parser fidelity and bounds against the
bundled runtime. `qualify_formats.py`, invoked by the existing installed ingestion
harness, checks exact database values, source preservation, replay, supported
snapshot paths, refusal of unsupported snapshots, late schema drift, bounds and
corruption, including page checksums beyond the preview. For each format, a disposable fixture holds its receipt table lock
after real COPY to test worker death/retry and cancellation before commit. This
uses ordinary PostgreSQL locks, with no runtime test hooks. Per-format reports
retain source hashes and sampled resource measurements, not user payloads.

Console browser qualification uploads all four new format modes, reviews mappings
and Parquet metadata, rejects a mismatched parser approval, verifies committed
PostgreSQL values and reconnects to the durable job after reload. These tests
run from `supabricks/console` against platform's exact relocated archives on
Linux x86_64 and macOS arm64 with external network denied. Native notebook,
environment, recovery and existing CSV failure suites remain required evidence.
Local engineering derivatives are development evidence only; final qualification
requires clean CI archives with the tested console source pin.
