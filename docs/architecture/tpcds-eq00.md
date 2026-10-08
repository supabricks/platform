# EQ00 — pinned inputs and native-schema admission

Status, 2026-10-06: input foundation, SF1 generation pilot and installed schema
admission audit completed. **Full PostgreSQL → sync → analytics testing has not
started.** Native-schema compatibility blocks it. SP remains
[frozen](sync-performance-freeze.md); this work neither merges its candidate nor
accepts its pending performance gates.

## Inputs and observed results

[Input lock](../../e2e/tpcds/inputs.lock.json) pins Databricks' TPC-DS kit at
`1b7fb7529edae091684201fab142d956d6afd881` (compiled version 2.13.0), source archive
and selected-file hashes, generator seed 19620718, SF1 and one generator child.
Apache Spark v4.2.0, commit `32f7299601108917fb01920a54e084595b7b3bf8`, supplies
all **103 SQL statements representing 99 templates**. Its test exclusions are
not inherited. SQL uses the checked-in literal substitutions; only surrounding
whitespace and terminal statement delimiters are removed for product admission.
This is a TPC-DS-derived engineering workload, not an official TPC-DS score.

| Check | Result |
| --- | --- |
| Complete business schema | 24 tables, native columns and keys retained |
| SF1 business rows | 19,557,335 |
| Generated files including separate run metadata | 1,253,240,746 bytes (1.253 GB / 1.167 GiB) |
| Generator elapsed, two successful invocations | 6.006 s / 6.006 s |
| Generation plus file validation, first successful invocation | 17.909 s |
| Repeatability | All 24 business files have identical SHA-256, bytes and row counts |
| Installed capture admission | 1/24 accepted; 23/24 rejected |
| Managed SQL lexical admission | 103/103 accepted; **0 executed** |
| Product/reference result coverage | 0/103; every statement remains `not_run` with a reason |
| Installed fixture cleanup | 30 descendants observed; zero leaked/remaining |

Generation used CPU affinity 0–3, a 4 GiB address-space limit, 300-second timeout,
16 GiB minimum free-space admission and a polled 4 GiB generated-file ceiling.
It is a single-process generator, not a four-core throughput measurement. The
ceiling can overshoot between polls. Every row was checked for field count,
complete delimiters and missing required values; exact field values, relationships
and query results still require EQ02. File counts/hashes cannot replace those
correctness checks. Generator timing is diagnostic, not an isolated comparative
performance result; it may overlap the short schema probe.

The first generator attempt failed before writing data: its upstream 80-byte
parameter limit caused a crash when passed the long absolute distributions path.
The same unmodified binary succeeds with relative `tpcds.idx`. The harness now
rejects overlong output paths. Retain the failed receipt and debug notes in
[provenance](tpcds-evidence/2026-10-06/provenance.json).

## Confirmed native-schema gaps

The unchanged installed `capture.source.inspect` was exercised against real
native PostgreSQL, with isolated positive/negative controls and each original
business-table DDL. The integer/decimal control passed; composite, DATE and CHAR
controls reproduced the following restrictions:

| Gap | Effect | Required work |
| --- | --- | --- |
| [#170: composite keys](https://github.com/supabricks/platform/issues/170) | Seven fact/inventory tables reject with `integer_primary_key_required` | Preserve multi-column identity through bootstrap, capture, apply, key-changing updates, deletes and replay |
| [#171: DATE / fixed-width CHAR](https://github.com/supabricks/platform/issues/171) | Six tables contain DATE; sixteen contain CHAR; sixteen single-key tables reject with `unsupported_source_column` | Qualify DATE and CHAR separately, including nulls, padding/comparison semantics and exact analytical types |

Only `income_band` passes native-schema admission unchanged. The
[per-table inventory](tpcds-evidence/2026-10-06/inventory.json) retains all columns,
types, nullability and primary-key components, with issue links. `dbgen_version`
is separately retained generator metadata, not a business table; its timestamp is
expected to differ between invocations. No business columns were converted or
omitted, and no surrogate keys were added.

The package was verified as engineering native overlay
`99590cc04aafdee52350c41d008fe375db44373354f07883f587d9425d3ba2fa`, corresponding
to the frozen baseline's native code. This **does not** qualify an exact release
archive. The baseline repository revision is
`8b68cd206edd5de2b1f820c90c39e7a76aedf3aa`; its exact release CI passed separately
in [run 37522400869](https://github.com/supabricks/platform/actions/runs/37522400869).

## Next stages and admission thresholds

Proceed through separately measured EQ01 changes: composite identity, DATE,
then CHAR. For each, test bootstrap, insert/update/delete, key changes where
applicable, restart/replay and source/analytical equality. Preserve an unchanged
integer/decimal control to distinguish compatibility from performance changes.
Zero correctness mismatches, lost/duplicated changes or leaked processes are
required. Admission alone does not qualify support.

The first product pilot is **SF1, one loader, one analytical stream**, 8 CPUs
(0–7) and a 16 GiB whole-product memory cap. Start sync before bounded COPY into
the enrolled tables: at most 1,024 rows and 4 MiB of encoded input per transaction,
whichever is reached first. Measure full-speed load and drain; do not require an
invented throughput threshold before measuring it. Use an initial two-hour
load/drain timeout and 120-second timeout per SQL statement, retaining failures.
Run the pinned Apache Spark reference separately against the same immutable data
boundary and consume complete outputs. These are pilot safety bounds, not query
latency promises. Freeze comparative latency thresholds and repetitions after the
first executable product/reference pilot, before qualification.

Still to establish in EQ01/EQ02: total installed storage admission (raw SF1 size
does not budget PostgreSQL, WAL, Delta, spill or retained versions), exact schema
budget and transaction semantics after compatibility changes, loader cancellation
and retry, full result consumption beyond SQL preview limits, and reference Spark
runtime/package identity. The current SQL preview has row/byte bounds; lexical
admission is not evidence that results are complete or semantics supported.
Console upload into an existing enrolled table remains a separate acceptance
case from PostgreSQL COPY. No claim is made that these paths are already qualified.

SF10/SF100 and the 4/8/16-CPU comparison follow SF1 correctness and explicit
capacity admission. They are not running. Return to SP only after reviewing the
E2E results, as directed by the user.

## Reproduction and retained evidence

[Harness instructions](../../e2e/tpcds/README.md) describe locked downloads,
generator build, bounded generation and installed admission. Five focused tests
cover tampered inputs, unsupported DDL, composite/decimal parsing, SQL delimiters
and malformed/truncated data. The fresh-download inventory also passed locally.

Committed receipts are in [the evidence directory](tpcds-evidence/2026-10-06/README.md),
including both successful generations, the failed first attempt, full schema/query
ledger, installed probe and cleanup. Raw generated files remain under
`/data2/supabricks-eq/eq00-20261006/`; generator sources/build logs and downloaded
inputs are under `build/eq00-20261006/`. Dataset bytes are not committed to Git.
