# EQ00 evidence — 2026-10-06

[Assessment and next gates](../../tpcds-eq00.md).

| Receipt | Scope |
| --- | --- |
| `inventory.json` | Every business column/key, separate generator metadata, issue-backed gaps and all 103 query statements still `not_run` |
| `compatibility.json` | Actual installed capture admission and lexical SQL checks; no end-to-end execution |
| `cleanup.json` | Native probe descendant accounting |
| `generation-failed-01.json` | Retained upstream long-path crash |
| `generation-02.json`, `generation-03.json` | SF1 rows/bytes, file hashes, bounds, generator timing and provenance |
| `provenance.json` | Build dependencies, native overlay/container identity, repeated-file equality, failed/debug attempt notes |

Verify receipts from this directory with `sha256sum -c SHA256SUMS`.
Raw datasets and original build logs are retained at paths recorded in
`provenance.json`; they are not included in Git. Generated business files match
across both successful invocations. No TPC-DS SQL has executed, no data has been
loaded into PostgreSQL, and no end-to-end performance envelope is qualified.
