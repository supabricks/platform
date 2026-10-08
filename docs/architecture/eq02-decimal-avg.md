# EQ02: Spark-compatible decimal AVG (#200)

The subsequent [decimal arithmetic correction (#199)](eq02-decimal-types.md)
raises qualification to 90/103. This report retains the AVG slice's original
78/103 result and paired measurements.

The candidate fixes all nine same-type decimal AVG mismatches in the retained
SF1 suite: **78 of 103 statements are now correct after explicit ordering review,
up from 69**. All 24 tables still match PostgreSQL exactly. The other 25 statements
remain tracked: nine memory failures (#197), four alias failures (#198), and
12 decimal type mismatches (#199). SP stays frozen; larger-scale qualification
remains open.

[Measured evidence](tpcds-evidence/2026-10-08-eq200/summary.json),
[baseline ledger](tpcds-evidence/2026-10-08-eq200/baseline/comparison.json), and
[candidate ledger](tpcds-evidence/2026-10-08-eq200/candidate/comparison.json)
retain every statement, full results, schemas, hashes, errors, and cleanup.
The source change is [Sail PR #2](https://github.com/supabricks/sail/pull/2),
commit `968537980e77c52d397df9553db536a8fda50fc6`, based on the previously
qualified `0ff69f29` tree rather than incorporating unrelated upstream changes.

## Cause and correction

DataFusion's decimal AVG rescales its sum and truncates the final integer
quotient. Spark rounds HALF_UP, with halfway ties away from zero. The unchanged
baseline reproduces all 133 one-unit decimal differences across q7, q9, q18,
q26, q27, q28, q57, q63, and q89. For example, q18 returns `101.832507` where
Spark returns `101.832508`.

Sail now applies exact HALF_UP finalization to Spark Decimal128 averages.
Ordinary, grouped, DISTINCT, window, `mean`, and DataFrame summary paths share
the implementation. The execution codec preserves it on workers. DataFusion's
accumulation, partial-state layout, grouping/filtering, retraction, signatures,
and non-decimal behavior remain in use. Rounding happens after merging sums and
counts, never by averaging rounded partial results. Checked rescaling and
precision bounds remain enforced; this does not redesign overflow semantics or
fix the separate decimal expression typing issues.

Five Rust regressions pass, including distributed-codec round-trip, grouping and
filtered state emission, DISTINCT partial merges, window retraction, signed ties,
nulls, and precision/overflow bounds. All 18 SQL tests pass on Spark 4.2.0, Sail
local, and Sail local-cluster. All 17 original reduced cases now match the
preserved Spark reference exactly; 12 mismatched before this change. Targeted
check, Clippy, formatting, 14 qualification-harness tests, and two source-artifact
contract tests pass.

## Retained-data qualification

Both runs use eight CPUs (0–7), 16 GiB, no swap or external network, the same
pinned container, original queries, 120-second statement bound, and preserved
Spark 4.2.0 reference. Both verify all **19,557,335 rows across 24 tables** and
attempt all 103 statements through managed product sessions at the same epoch.
No SF1 reload, query rewrite, numeric tolerance, or type coercion is involved.

The baseline release is
`286d92c5ccbfe18689d61528ec092053a4815c20655b9b78ace706080190e12c`.
The candidate is
`6d365fc4d04dbe71b4447eb6c405e9fb1665d7472eec660a8d7cf35a964ec70e`.
The source-built wheel SHA-256 is
`7e5ad2dcfa7ac258fbff9251646bbebcb31b7d70e0828c79dab4ce0d2fe770b3`.

The candidate changes seven Sail payload/provenance files from the recovered
native baseline; its native binary, Delta runtime, and other payloads are exact.
A stopped copy of the successful load receives the normal installation upgrade
with an explicit backup. The verifier checks artifact-bound file contents,
original load identity, upgrade journal, backup, applied runtime identity, and
publication epoch. The original load receipt remains
`0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f`.
This is an unsigned engineering qualification, separate from release archive CI.

| Reviewed result | Baseline | Candidate |
| --- | ---: | ---: |
| Correct | 69 | 78 |
| Decimal value mismatch | 9 | 0 |
| Decimal type mismatch | 12 | 12 |
| Execution failure | 13 | 13 |

No previously correct statement regresses. Both runs have zero leaked or
remaining descendants (278 observed baseline, 283 candidate). The baseline
post-processing initially stops on a previously unseen q79 tie permutation.
Its explicit review proves the same typed row multiset, selected rows, and
ordered sequence of all four SQL ordering keys. Only tied rows differ. q56 is
strictly identical in that baseline repeat. The review helper now recognizes
five named ordering cases; every accepted case independently proves its rows
and keys. Original strict ledgers and the initial post-processing failure stay
in the evidence. No general unordered comparison is introduced.

## Measured contribution

The nine affected statements take **11.601 seconds before and 11.618 seconds
after** in total (about +0.15%). These are client-process query/result timings;
session startup is recorded separately. This single paired run shows the
correctness contribution without an observed material latency change; it does
not establish a statistical speedup or throughput result. Fixed run order and
host/page-cache effects are not isolated. Full verification, query execution,
and cleanup take 462.694 and 463.005 seconds respectively.

| Statement | Baseline seconds | Candidate seconds | Candidate vs Spark |
| --- | ---: | ---: | --- |
| q7 | 1.084 | 1.073 | Exact match |
| q9 | 2.611 | 2.596 | Exact match |
| q18 | 1.002 | 1.010 | Exact match |
| q26 | 0.879 | 0.858 | Exact match |
| q27 | 1.119 | 1.128 | Exact match |
| q28 | 1.952 | 1.940 | Exact match |
| q57 | 0.996 | 1.016 | Exact match |
| q63 | 0.966 | 0.971 | Exact match |
| q89 | 0.992 | 1.025 | Exact match |

Reproduction commands, packaging proof, source/build lock, tests, both complete
query ledgers, explicit ordering reviews, raw result/error files, and cleanup
reports are in the [evidence directory](tpcds-evidence/2026-10-08-eq200/).
The [qualification harness](../../e2e/tpcds/README.md#qualifying-a-sail-correction-without-reloading-sf1)
documents the explicit analytical-candidate upgrade path. Next, qualify #199,
#198, and #197 as separate measured changes against this same retained SF1 data.
