# EQ02 SF1 end-to-end results

The full SF1 PostgreSQL load and exact Delta comparison pass. The
[correlated CHAR correction (#198)](eq02-correlated-char.md) qualifies **101 of
103 statements after explicit ordering review**, up from 97 with the
[analytical resource profile (#197)](eq02-query-resources.md). All 103 now execute;
only the two DOUBLE variance mismatches (#206) remain. Every previously correct
statement stays correct. SP stays frozen. No larger-scale or signed-release
qualification is claimed.

The original baseline below remains unchanged: 69 correct, with 34 tracked
failures or mismatches. The separate [#200](eq02-decimal-avg.md) and [#199](eq02-decimal-types.md) reports retain their paired baseline
and candidate reruns, exact results, and per-query timings.

[Machine-readable evidence](tpcds-evidence/2026-10-07-eq02/snapshot-recovery/summary.json),
[original strict comparison](tpcds-evidence/2026-10-07-eq02/snapshot-recovery/comparison.json),
and [explicit discrepancy review](tpcds-evidence/2026-10-07-eq02/snapshot-recovery/review.json)
retain the full disposition, query hashes, schemas, results, and errors.

## Load and exact data

Attempt 11 publishes all **19,557,335 rows across 24 tables** in 4,431.262 seconds
(73.9 minutes, approximately 4,413 rows/s including startup and cleanup).
Continuous sync is healthy before the first COPY. Original SF1 files, Latin-1
import, transaction bounds, publication window, eight CPUs, 16 GiB/no swap,
768 MiB apply-worker limit, generation/retention limits, and two-hour deadline
are unchanged. This is one successful engineering load, not a repeated scaling
or sustained-freshness qualification.

All 1,373 publication receipts remain available. Two compactions take 161.319
seconds; the final generation is 761,416,771 bytes. The maintained sampler sees
134 apply workers, a 525,434,880-byte peak and zero sampling errors. Sampling can
miss transient peaks. Load cleanup observes 254 descendants and leaves zero.
See [capacity and compaction analysis](eq02-compaction-capacity.md).

Exact comparison passes for every PostgreSQL/Delta table, including the
11,745,000-row inventory table and all three sales facts. Partitioned SHA-256
multisets preserve duplicate multiplicity and typed values, including NULLs,
exact decimals, dates, Unicode and CHAR padding. Schemas and CHAR metadata are
checked separately. Row counts alone are not the acceptance test.

## Retained-state restart correction

The first verification restart fails before checking data: synchronous retained
publication recovery delays child authorization while storage readiness probes
already run. The probes kill unauthorised children after ten seconds; the broker
survives long enough to start after recovery. This is tracked in
[#196](https://github.com/supabricks/platform/issues/196).

Commit `a721740` fences the old supervisor, recovers captures, sessions and
publications, and only then starts new engine children. Authorization and
readiness checks remain enabled. Thirteen analytical publication tests,
33 daemon/recovery tests and 12 qualification-harness tests pass. The retained
full-SF1 restart then reaches all exact table and analytical checks.

The successful 74-minute load is not repeated or relabeled. Its original receipt
and state remain retained. A stopped copy receives the supported explicit
native-only installation upgrade, with a mandatory verified backup and an
engineering version increase. All workers and dependencies remain identical.
The verifier requires the original manifest, exact native-only inventory change,
completed upgrade receipt and matching runtime identity; default same-release
verification stays strict.

- Load release: `6b54ce28bc601f3ab46c78475f11d380b07f777a56e26987c5f18b1d9433a724`.
- Verification release: `286d92c5ccbfe18689d61528ec092053a4815c20655b9b78ace706080190e12c`.
- Unchanged load receipt: `0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f`.

Copying also changes the worktree inode. The product correctly requires an
explicit `project attach` to its existing deployment. That supported command
runs during the verifier's health wait without restarting the active run or
changing source data. The published epoch remains unchanged. The total
630.131-second verification duration includes this intervention and is not a
startup performance measurement. Final cleanup observes 288 descendants and
leaves zero; no supervisor timeout occurs.

## Complete analytical coverage

All 103 original statements are attempted through managed Supabricks/Sail
sessions. Ninety execute, and 13 fail. The independent Apache Spark 4.2.0 run
already completed all 103 against the same generated input and type profile.
Each successful product result is compared in full, without floating tolerance,
type coercion, decimal rounding, SQL rewrites, or sampling result rows.

| Disposition | Statements | Tracking |
| --- | ---: | --- |
| Strict type/value/order match | 65 | Original comparison |
| Correct after explicit SQL ordering review | 4 | q24a, q56, q64, q77 |
| Query memory exhaustion | 9 | [#197](https://github.com/supabricks/platform/issues/197) |
| Missing projected column aliases | 4 | [#198](https://github.com/supabricks/platform/issues/198) |
| Decimal precision/scale mismatch | 12 | [#199](https://github.com/supabricks/platform/issues/199) |
| Same-type decimal value mismatch | 9 | [#200](https://github.com/supabricks/platform/issues/200) |

The four ordering reviews retain identical typed row multisets. q24a has no
ORDER BY or LIMIT. For q56, q64 and q77, both engines return the same selected
rows in the required key order; differences occur only within ties. The original
strict ledger remains unchanged, and the review records the named clauses,
query/result hashes and executable assertions. It is not a general waiver for
ordering differences.

Memory failures use the installed Sail **256 MiB query pool**, which is separate
from the container's 16 GiB allocation and the sync worker's 768 MiB ceiling.
Affected statements are q21, q39a, q39b, q47, q66, q67, q72, q75 and q78. Hash
joins and aggregate/sort spill reservations exhaust the pool. Both resource
configuration and plan/spill behavior need investigation before increasing a
limit in a separately declared run.

Alias failures affect q6, q30, q41 and q81: expressions still reference original
column names while the projected fields have generated numeric names.

Decimal type differences affect q2, q12, q20, q31, q36, q49, q58, q59, q61, q83,
q90 and q98. For example, q12 returns decimal(34,6) versus Spark decimal(38,17).
Same-type value differences affect q7, q9, q18, q26, q27, q28, q57, q63 and q89.
All 133 observed differing values are exactly one unit of their decimal scale;
examples show AVG truncation where Spark rounds. They remain failures, not
floating-point tolerance candidates.

## Next qualification slices

The decimal AVG (#200), arithmetic (#199), resource profile (#197), and
correlated CHAR (#198) candidates pass their retained-SF1 checks. Next fix the newly
exposed q39 variance differences (#206). Rerun the full 103-statement suite
against the same retained data and Spark reference after each logical slice. Do not reload SF1 to test query-only changes. EQ03 freshness/recovery,
EQ04 size/resource scaling and EQ05 concurrency remain unqualified.

The previous CI head `9ee327b` passes 33 jobs, including both release-sync gates,
but macOS notebook recovery fails (#136). Candidate archive CI is a separate
merge gate; local engineering success does not supersede it. PR #183 remains a
draft, stacked on #177. Nothing in this result resumes or merges frozen SP.
