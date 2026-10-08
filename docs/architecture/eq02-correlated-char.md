# EQ02: correlated CHAR aggregate keys (#198)

Four original SF1 statements—q6, q30, q41 and q81—fail when a correlated
aggregate refers to a CHAR field after projection pushdown has replaced its
qualified name. This slice corrects the Sail optimizer and holds the
[#197 analytical resource profile](eq02-query-resources.md) fixed. SP stays frozen.

The full paired SF1 qualification executes all 103 statements and increases
correct results from **97 to 101 of 103**, with no previously correct statement
regressing. All four targeted statements are now correct. The two remaining
DOUBLE variance mismatches, q39a and q39b, remain tracked in
[#206](https://github.com/supabricks/platform/issues/206); they are not accepted
through a tolerance change. The subsequent [#206 review](eq02-variance.md)
records the source correction and separately approved numerical contract; the
measurements here remain the historical #198 pair. The reduced regressions also pass in both local
and local-cluster Sail, matching the independent Spark reference.

## Cause and correction

Imported Delta CHAR fields carry Spark raw-type metadata. Sail's CHAR analyzer
pads comparison operands to their common width, including correlated equality
predicates. DataFusion's scalar-aggregate decorrelator recognizes a local column
or a cast of a column as a grouping/join key, but not `rpad(column, width, ' ')`.
Decorrelation is deferred. Subsequent filter/projection pushdown changes field
names; another decorrelation attempt then cannot resolve the original qualified
column. Ordinary STRING control cases do not encounter this path.

The Sail optimizer now materializes those padded equality keys before
DataFusion decorrelation. Repeated expressions share one internal projection
column. A restoring projection keeps internal keys out of the visible schema.
Only deterministic space-padding of a local column against an outer-only
expression is admitted; arbitrary functions, fallible casts, inequalities and
uncorrelated filters remain unchanged.

An explicit STRING cast has a related barrier: CHAR analysis wraps it in a
metadata alias to suppress padding. After analysis, that alias can be removed
from the correlated predicate, exposing the safe string-to-string cast that
DataFusion already understands. The cast itself and its comparison semantics
remain. This does not change metadata on selected output fields.

The source fix is in [Sail PR #4](https://github.com/supabricks/sail/pull/4),
with the pinned artifact and qualification in
[platform PR #209](https://github.com/supabricks/platform/pull/209).
There are no query-specific optimizer branches or rewritten TPC-DS statements.

## Focused validation

The 26 SQL cases use six rows with exact DECIMAL values, NULL, empty strings,
Unicode, and different CHAR widths. The product fixtures import actual Delta
tables and versioned views. Independent Apache Spark 4.2.0 uses the same values
and raw-type metadata in native DataFrames and views. Both engines execute the
same SQL with the same expected results.

| Engine | Passing cases |
| --- | ---: |
| Previous Sail source | 4/26 |
| Independent Spark 4.2.0 | 26/26 |
| Candidate Sail, local | 26/26 |
| Candidate Sail, local-cluster | 26/26 |

Cases cover scalar AVG/COUNT, reversed correlation, multiple keys, empty matches,
NULL, EXISTS/NOT EXISTS controls, OR factoring, explicit STRING casts, grouped
CTEs, and visible output schemas. Three Rust optimizer regressions also pass,
including key deduplication, repeated application, and non-admitted expressions.
Clippy and Python lint pass. The existing 53 CHAR/decimal regression cases pass
in both local and local-cluster modes.

Two preliminary candidates passed 24/26 cases but failed the explicit STRING
cast cases with physical/logical metadata disagreement. They are rejected and
their reports retained. Only the final source enters full SF1 qualification.

The initial DataFrame-only reproducer also exposed a separate Spark Connect
metadata issue: Sail did not apply imported CHAR semantics consistently to
DataFrame fields. It is tracked in
[#208](https://github.com/supabricks/platform/issues/208). The Delta-backed
regressions above exercise the product path relevant to #198; they do not claim
to resolve #208.

## Qualification contract

The original successful SF1 load remains retained: 19,557,335 rows in 24 tables,
epoch `fed0899c-c267-4d8a-8432-3e5e920718af`, and load receipt SHA-256
`0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f`.
A fresh stopped copy receives a verified backup and supported installation
upgrade. No data reload is performed.

Both full runs use eight CPUs, a 16 GiB container with no swap or external
network, and the same explicit analytical profile: 2 GiB query pool, 8 GiB spill,
6 GiB sampled worker RSS ceiling and 1 GiB spill-file limit, with join reordering
enabled. Per-statement timeout and result limits remain 120 seconds and 16 MiB.
All 24 typed PostgreSQL/Delta comparisons and all 103 original statements run
through managed product sessions. The existing independent Spark reference and
strict comparator are unchanged.

The before-run verifies the previously qualified source pin through an explicit
read-only mount of its retained lock file; the after-run verifies the new pin.
Both verify the same native/session-worker artifact from #197. Installed payload
inventories admit only the source-bound Sail change between these two candidates.
The original-load-to-candidate verifier separately admits the exact previously
qualified native/session-worker overlay. This is an engineering qualification,
not a signed release or a larger-scale claim.

## Full paired SF1 results

| Disposition | Before | After |
| --- | ---: | ---: |
| Statements attempted | 103 | 103 |
| Statements executed | 99 | 103 |
| Correct after explicit ordering review | 97 | 101 |
| Execution failures | 4 | 0 |
| Strict value mismatches | 2 | 2 |

All 24 tables pass exact typed PostgreSQL/Delta comparison in both runs. The
original load receipt and epoch remain identical. Both supervised runs stop
cleanly with zero leaked or remaining descendants. The strict comparator is
unchanged; any accepted ordering differences are individually documented in
the retained review with SQL/result hashes, exact typed multisets, and sort-key
assertions. q6 orders by count alone, so differences within equal-count ties
are permitted only when both selected multisets and sorted count keys agree.

| Formerly blocked statement | Candidate seconds |
| --- | ---: |
| q6 | 0.887 |
| q30 | 0.793 |
| q41 | 0.778 |
| q81 | 0.848 |

The 97 previously correct statements total **118.560 → 118.507 seconds**
(-0.05%) in this single pair. Full verification, including startup,
table checks and cleanup, takes **472.756 → 471.519 seconds**.
These are descriptive measurements, not a statistically established speedup.
Failed baseline queries are not used as speedup denominators.

| Sampled worker resource peak | Before | After |
| --- | ---: | ---: |
| RSS | 2619.5 MiB | 2783.7 MiB |
| Owned spill | 84.0 MiB | 88.1 MiB |
| Largest spill file | 10.6 MiB | 10.6 MiB |

Samples are taken every 100 ms and may miss brief peaks. Spill figures are
peak live owned bytes, not cumulative I/O. The profile and its limits are
identical across the pair.

Candidate Sail source: `a04ab123d5ad6fa6b28fdb7ad36d2411859652e8`.
Wheel SHA-256: `6d1d2b9c0669a5fedc268681102a2a0783ec9993f63452b5cb4037de63a1aa1c`.
Installed candidate identity: `d0bd4a4c9839c87e497cdee12b9be1bc8ddabf1cc405217bc3538a28ad7956c0`.
The native/session-worker source remains
`0280d26919ad47b71defc2ea43b23813e5523030` from #197.

[Machine-readable summary](tpcds-evidence/2026-10-08-eq198/summary.json),
[strict comparison](tpcds-evidence/2026-10-08-eq198/candidate/comparison.json),
[explicit result review](tpcds-evidence/2026-10-08-eq198/candidate/review.json),
and [SHA-256 inventory](tpcds-evidence/2026-10-08-eq198/SHA256SUMS)
retain diagnostic optimizer traces, typed rows, per-query timing/resource samples, table
comparison receipts, focused tests, build provenance and rejected diagnostics.

These local results do not replace release CI. A separate offline catalog
startup failure in #207 is tracked in
[#210](https://github.com/supabricks/platform/issues/210); its missing startup
diagnostics must not be treated as proof of an analytical-profile regression.
