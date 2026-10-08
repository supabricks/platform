# EQ02: Spark-compatible decimal arithmetic (#199)

Subsequent [resource-profile qualification (#197)](eq02-query-resources.md) reaches
97/103 correct. This report preserves the earlier decimal-arithmetic comparison.

The candidate fixes all 12 decimal precision/scale mismatches in the retained
SF1 suite: **90 of 103 statements now match Spark after explicit ordering review,
up from 78**. All 24 tables still match PostgreSQL exactly. The remaining 13
statements are nine memory failures (#197) and four alias failures (#198).
SP remains frozen; larger-scale qualification remains open.

[Measured evidence](tpcds-evidence/2026-10-08-eq199/summary.json),
[baseline ledger](tpcds-evidence/2026-10-08-eq199/baseline/comparison.json), and
[candidate ledger](tpcds-evidence/2026-10-08-eq199/candidate/comparison.json)
retain every statement, exact results, schemas, hashes, errors, and cleanup.

## Cause and correction

Sail previously delegated decimal binary arithmetic to DataFusion. Its division
formula retained fewer fractional digits than Spark, mixed integer literals
used the integer type's full precision, and ROUND retained the input precision
when reducing scale. Casting the final output would change the schema without
recovering fractional digits already lost in intermediate expressions.

The source fix applies Spark's result-type formulas before execution, gives
integral literals their minimum required precision, and uses exact integer
arithmetic with HALF_UP rounding at the result scale. Checked i128 arithmetic
handles ordinary values; arbitrary-precision intermediates handle calculations
that exceed i128 but have a representable result. Decimal overflow and division
by zero respect ANSI mode. The precision-loss setting is carried from the Spark
session into each operation. ROUND uses Spark's decimal result precision while
retaining the existing evaluation kernel. The worker codec preserves the
operation and settings.

The semantics follow Spark 4.2.0's
[arithmetic expressions](https://github.com/apache/spark/blob/v4.2.0/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/expressions/arithmetic.scala),
[decimal operand coercion](https://github.com/apache/spark/blob/v4.2.0/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/analysis/DecimalPrecisionTypeCoercion.scala), and
[ROUND](https://github.com/apache/spark/blob/v4.2.0/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/expressions/mathExpressions.scala).
The source PR is [Sail #3](https://github.com/supabricks/sail/pull/3).

The first full-suite candidate fixed all 12 original type mismatches but exposed
three regressions (q1, q32, q92): DataFusion decorrelation temporarily replaces
an aggregate with an untyped NULL when evaluating its empty-input result.
The new decimal UDF initially rejected that intermediate operand. Five reduced
correlated-aggregate tests reproduce the failure. Decimal coercion now accepts
these null operands; the rejected first run remains in the evidence.

## Qualification

Both full-suite runs use the existing SF1 load: 19,557,335 rows across 24 tables,
the original publication epoch and load receipt, and the preserved independent
Spark reference. Each uses eight CPUs, 16 GiB, no swap or external network, and
the same 120-second statement bound. No queries or numeric comparisons are
relaxed. The baseline is measured before changing the source pin; the candidate
is packaged with the verified source artifact and applied to a stopped copy
using the normal backup/upgrade path. The original successful load is retained.

The source is `528b49dac7beafa6d5a0e1f2e538efcbbc4aca2f`. The source-built wheel SHA-256 is
`6d458b772511bb5b9acbe4d77fab4a018cff8cd89064a8c45cfec73ea04bbf1c`. The installed baseline is
`6d365fc4d04dbe71b4447eb6c405e9fb1665d7472eec660a8d7cf35a964ec70e` and the candidate is
`1208de89e46af8ae20ab62f3378ccf92082072564c53dc792449a70277021cee`. The candidate changes six Sail
payload/provenance files; the native binary, Delta runtime, and other payloads
are unchanged from the qualified AVG baseline. This is an unsigned engineering
qualification, distinct from release archive CI.

The original load receipt remains
`0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f` and the publication epoch remains
`fed0899c-c267-4d8a-8432-3e5e920718af`. The verifier checks the source-bound artifact,
installation files, completed upgrade journal, backup, and applied runtime
identity. Source lock and build reports for both candidate attempts are retained.

| Reviewed result | Baseline | Candidate |
| --- | ---: | ---: |
| Correct | 78 | 90 |
| Decimal type mismatch | 12 | 0 |
| Decimal value mismatch | 0 | 0 |
| Execution failure | 13 | 13 |

No previously correct statement regresses. Both runs have zero leaked or
remaining descendants. Original strict comparison ledgers are retained;
the existing explicit ordering review checks identical typed row multisets,
selected rows and ordering keys for the five named SQL tie/unordered cases.
There is no general unordered comparison or numeric tolerance.

## Regression coverage

All **52 SQL tests** pass independently on Spark 4.2.0, Sail local, and Sail
local-cluster: 34 decimal arithmetic regressions plus the 18 earlier AVG tests.
They cover types and values, mixed integer literals, positive and negative
HALF_UP ties, wide intermediates, overflow/zero behavior with ANSI on and off,
precision-loss settings, ROUND, null operands, nested/window aggregates,
and correlated aggregate empty-input evaluation. All **47 reduced reference
cases** now have the expected outcome, versus 24 on the baseline: 43 exact
result comparisons and four expected out-of-range cast errors. The five added
correlation cases separately reproduce the rejected candidate's regression.

Three Rust arithmetic tests pass on the final source. Two worker-codec checks
passed on the initial source; that codec is unchanged by the null fix, and the
final installed worker suite passes. Targeted Clippy and formatting, Python
lint, 14 qualification-harness tests, 40 component tests, and two source-artifact
contract tests pass. The failed first full run and reduced null reproduction
remain under `first-candidate/` in the evidence.

## Measured contribution

The 12 affected statements take **12.747 seconds before and
12.655 seconds after** in total (-0.72%). These are client-process
query/result timings; session startup is retained separately. This paired run
measures the correctness contribution; it does not establish a statistical
speedup or throughput result. Fixed run order and host/page-cache effects are
not isolated. Full table verification, queries and cleanup take
463.178 and 464.355 seconds respectively.

| Statement | Baseline seconds | Candidate seconds | Candidate vs Spark |
| --- | ---: | ---: | --- |
| q2 | 0.938 | 0.930 | Exact match |
| q12 | 0.844 | 0.823 | Exact match |
| q20 | 0.931 | 0.907 | Exact match |
| q31 | 1.268 | 1.277 | Exact match |
| q36 | 0.997 | 1.002 | Exact match |
| q49 | 1.102 | 1.081 | Exact match |
| q58 | 1.197 | 1.193 | Exact match |
| q59 | 1.334 | 1.329 | Exact match |
| q61 | 1.364 | 1.366 | Exact match |
| q83 | 0.837 | 0.835 | Exact match |
| q90 | 0.864 | 0.849 | Exact match |
| q98 | 1.070 | 1.062 | Exact match |

Commands, source/build locks, package and upgrade proofs, tests, full query
results, reviews, and cleanup reports are in the
[evidence directory](tpcds-evidence/2026-10-08-eq199/). The baseline command was
run with source pin `96853798` before updating the platform lock; its original
source lock is retained separately. Reproduction requires that baseline pin
and fresh output directories, then the candidate pin and stopped-copy upgrade.
The [harness guide](../../e2e/tpcds/README.md#qualifying-a-sail-correction-without-reloading-sf1)
describes the artifact-bound upgrade and verification contract.

Next fix #198's four alias failures, then #197's nine query resource failures
as separately measured slices. EQ03 freshness/recovery, EQ04 size/resource
scaling, and EQ05 concurrency remain unqualified. Do not reload SF1 for
query-only corrections.
