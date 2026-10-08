# EQ02: Spark variance and explicit SF1 q39 numerical review

Issue [#206](https://github.com/supabricks/platform/issues/206) covers the remaining
q39a/q39b coefficient-of-variation differences after the correlated-query fix.
The source correction implements Spark's update and merge arithmetic for variance
and standard deviation, including distributed workers and bounded windows. The
user approved an independent-oracle qualification contract on 2026-10-08. This
contract applies only to the retained SF1 q39 fixture; the general strict comparator
and original SQL/reference remain unchanged.

The fresh full pair executes all 103 statements, passes all 24 exact typed table
checks, and has no previously exact result regressions. **101 statements match
exactly after explicit ordering review; q39a/q39b satisfy the separately approved
numerical contract.** All 103 are therefore accepted under the stated engineering
contract. This is not 103 bit-identical results, and is not an official TPC-DS score.

Both baseline and candidate q39 results meet the independent bound. The source
change corrects Spark arithmetic and edge-case semantics; the acceptance contract
resolves partition-order numerical ambiguity. Those are separate contributions.

## Root cause and correction

For count `n`, running mean `mean`, and second central moment `M2`, Spark updates:

```text
n = n + 1
delta = value - mean
delta_n = delta / n
mean = mean + delta_n
M2 = M2 + delta * (delta - delta_n)
```

DataFusion's prior update used `delta * (value - new_mean)`. These expressions
are equivalent over real numbers, but intermediate double rounding differs.
Spark's merge expression also has a specific multiplication order. Sail now
preserves both orders, using floating-point count/mean/M2 state as Spark does.
The implementation follows Spark 4.2's
[CentralMomentAgg source](https://github.com/apache/spark/blob/v4.2.0/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/expressions/aggregate/CentralMomentAgg.scala).

The implementation covers `var_samp`, `variance`, `var_pop`, `std`, `stddev`,
`stddev_samp`, `stddev_pop`, window aggregates, and the summary-statistics stddev
path. It preserves null skipping, empty/sample-singleton NULLs, DISTINCT state,
group filters, partial aggregation, and memory accounting. Population variance
and standard deviation of a singleton infinity now return NaN, matching Spark;
the previous implementation returned zero.

Ordinary scalar/grouped aggregates retain constant state per group. Bounded
sliding windows retain nonnull frame values and recompute moments after rows
leave the frame. Inverse updates have different rounding and cannot recover
when NaN or infinity leaves a frame. This brings a material cost: O(frame size)
state and O(frame size) work after a retraction. The SF1 qualification does not
establish a bounded-window performance improvement.

Local-cluster testing found an additional required path: remote-plan serialization
must encode the custom UDAF. Otherwise workers reconstruct DataFusion's built-in
function by name and silently restore the previous behavior. All four canonical
functions now round-trip through Sail's codec; a Rust test and distributed SQL
checks cover this boundary.

## Why bit-for-bit agreement is insufficient

Spark itself changes its last floating-point bits when partitioning changes. For
`[10, 404, 13, 814]`, retained one-partition and two-partition Spark diagnostics
produce sample variance `147020.25000000003` and `147020.25`, respectively. Both
are valid floating-point evaluations. Matching one retained Spark ordering does
not prove that other orderings are wrong.

q39 joins January and February inventory groups after filtering coefficients of
variation. A permissive generic relative epsilon could hide a changed predicate,
row membership, join, mean, or type. This review therefore uses a separate,
fixture-specific acceptance procedure rather than changing `compare.py`.

## Numerical contract

The review pins the independent Spark receipt, original q39 SQL hashes, original
load receipt and publication epoch, and a read-only extraction of **90,000 groups
/ 360,000 inventory rows** for January/February 2001. Item, warehouse and selected date keys are
unique and all extracted inventory keys reference those dimensions. All 24
PostgreSQL/Delta typed table checks must also pass in each product run.

For each group, exclude NULL quantities and calculate exact integers:

```text
n = count(x)
s = sum(x)
t = sum(x*x)
CV² = n * (n*t - s*s) / ((n-1) * s*s)
```

Groups with fewer than two nonnull values or zero mean cannot satisfy the q39
predicate. For the others, compare the numerator and denominator as integers
for `CV > 1`, and compare `4*numerator > 9*denominator` for `CV > 1.5`.
Apply both months' predicates and the exact self-join before inspecting outputs.
This independently determines **243 q39a rows and 14 q39b rows**, including their
order. Exact mean is `s/n`; the input sums/counts in this fixture are representable
without integer-to-double loss.

Only zero-based columns **4 and 9** (DOUBLE coefficients of variation) receive a
numerical review. Every value in both candidate and reference, including cells
that already match, must be finite, positive, and within **two representable-double
steps (ULPs)** of the nearest double to the independent 80-digit decimal square
root. A second calculation at 160 digits must round to the same double. Exact
rational comparisons of CV² with the squared neighboring-double midpoints also
certify that this is the nearest double, independently of Decimal rounding.
Keys, means, positional SQL types, row membership, duplicate counts and ordering
remain exact. The raw strict comparison and every differing cell remain retained.

The bound is supported by a finite sensitivity experiment on all 486 distinct
selected groups: enumerate every permutation of their two to four nonnull
values and every nonempty contiguous partitioning; apply Spark's update and
sequential final merge into a zero buffer. The largest error is two ULPs. Five
groups can reach two ULPs; the retained Spark reference has one two-ULP cell.
This is an explicit engineering contract for this dataset, **not** an error theorem
for arbitrary doubles, scales, merge trees, or SQL. A future dataset requires a
new reviewed contract. It is not a claim of 103 bit-identical results.

## Reproduction and evidence

Run `e2e/tpcds/compare.py` first, unchanged. Then run
`e2e/tpcds/q39_review.py --product PRODUCT --reference REFERENCE --groups GROUPS
--strict COMPARISON --output FRESH_DIRECTORY`. `GROUPS` accepts the retained JSON
or its gzip archive. The review checks pinned provenance, recomputes the strict
ledger, derives the integer membership oracle, and emits every numerical cell.
It reports q39 acceptance separately; other queries still require their strict
result or an individually justified ordering review.

The retained full pair uses the analytical profile from #197: 8 container CPUs,
16 GiB container memory with swap disabled; 2 GiB query pool, 8 GiB spill quota,
6 GiB sampled worker RSS limit, 1 GiB single-file limit, and join reordering.
It reuses the same 24-table, 19,557,335-row load and publication epoch, original
103 SQL statements, 120-second query timeout, 16 MiB harness result cap and
independent Spark 4.2 reference. The candidate uses the supported installation
upgrade with a verified backup of a fresh copy of the original load. No reload
or query rewriting is involved.

After checking the evidence checksums, the published rows can be reviewed without
a running stack:

```sh
python3 docs/architecture/tpcds-evidence/2026-10-08-eq206/replay-evidence.py \
  --repo . --output /tmp/eq206-fresh-replay
```

The replay uses the previously published independent Spark reference and compares
both regenerated reviews with their retained reports. The output must be fresh.

An existing generated-column-name difference is separately tracked in
[#211](https://github.com/supabricks/platform/issues/211): Sail omits DISTINCT and
FILTER from unnamed aggregate expressions. Both predecessor and candidate exhibit
it; values and q39's explicit aliases are unaffected. The reduced baseline, Spark
and candidate schemas are retained in this slice's evidence.

## Full paired results

| Measure | Baseline | Candidate |
| --- | ---: | ---: |
| Executed statements | 103 | 103 |
| Exact after explicit order review | 101 | 101 |
| q39 statements accepted through numerical review | 2 | 2 |
| Previously exact regressions | — | 0 |
| Exact PostgreSQL/Delta table comparisons | 24 | 24 |
| Previously exact 101 query seconds | 123.387 | 123.261 |
| All 103 query seconds | 125.098 | 124.974 |
| Full qualification seconds | 474.705 | 474.889 |
| Peak sampled worker RSS bytes | 2,733,342,720 | 2,747,547,648 |
| Peak live spill bytes | 88,093,328 | 88,093,264 |
| Largest sampled spill file bytes | 11,105,672 | 11,105,672 |

These are descriptive measurements from one sequential pair on the same host,
not a statistically established speedup. Resource samples are every 100 ms;
brief peaks can be missed. Spill is peak live owned bytes, not cumulative I/O.
Both runs shut down with zero leaked or remaining descendants.

| q39 statement | Baseline raw differing cells | Candidate raw differing cells |
| --- | ---: | ---: |
| q39a | 60 | 67 |
| q39b | 8 | 9 |

Each of the 514 CV cells in each engine is checked against the oracle, including
matching cells. Both product runs are within one ULP; the Spark reference has one
two-ULP cell. Matching Spark's arithmetic does not guarantee fewer strict last-bit
differences: q39a differs in 60 baseline cells and 67 candidate cells, while both
remain within one ULP of the independent oracle. No improved numerical accuracy
or bit-for-bit determinism is claimed. The numerical ledger retains their exact strings, nearest-double
oracle, high-precision decimal and ULP distance. The strict ledger still records
q39 value mismatches. Other order-only differences have the retained per-query
SQL, type, exact multiset and sort-key proofs inherited from the earlier slices.

Validation: 316 sail-function Rust unit tests, the new remote-codec round-trip
test, Clippy on sail-function/sail-plan/sail-execution, 13 focused SQL cases each
on Spark 4.2, local Sail and local-cluster Sail, and 28 TPC-DS harness tests.
The focused suite includes grouped/filter/alias, partial merge, DISTINCT,
null/empty/singleton, nonfinite and moving-window cases.

Two preliminary candidates were rejected: the first lacked bounded-window
support; the second passed local tests but omitted remote UDAF serialization.
Their build receipts and failure reports are retained. They are not qualified
artifacts or performance baselines.

Qualified Sail source: `0aa409adf865a324ef33f834628fd8430d2bc966`.
Wheel SHA-256: `e718b9a478915bd3fa91412817e1a34cff408c96854ecf1c2685290948997c9c`.
Installed candidate identity: `cbffbb7c4fec7517f56c822fa0e829df76dbbd0dec1e9744f2b3cd0e9d8b2166`.
The native binary/session-worker source stays at
`0280d26919ad47b71defc2ea43b23813e5523030` from #197.

[Summary](tpcds-evidence/2026-10-08-eq206/summary.json),
[strict candidate ledger](tpcds-evidence/2026-10-08-eq206/candidate/comparison.json),
[numerical review](tpcds-evidence/2026-10-08-eq206/candidate/numerical-review/review.json),
[ordering review and raw differing cells](tpcds-evidence/2026-10-08-eq206/candidate/review.json),
and [SHA-256 inventory](tpcds-evidence/2026-10-08-eq206/SHA256SUMS)
retain the provenance, typed query rows, high-precision oracle, complete grouped
input extraction, diagnostics, test logs and per-query resource/timing samples.

These local results do not replace release CI or larger-scale qualification.
SP remains frozen. No claim is made for another TPC-DS scale factor or arbitrary
floating-point input.
