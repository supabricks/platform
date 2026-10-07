# EQ02 SF1 load pilot evidence

See [the assessment](../../eq02-sf1.md). Attempts 01–04 failed and remain in the
record. The fourth establishes the apply-worker memory blocker #182.

Each attempt includes its result, supervisor cleanup, last published descriptor,
compressed commit/observation ledgers and output log. State directories, source
credentials and database files remain private under
`/data2/supabricks-eq/eq02-20261007/`; they are not committed.

`load-utf8.py` is the exact loader used in attempts 01/02;
`load-latin1-unpaced.py` is the exact loader used in attempt 03. Their hashes match
the fixture identities in those receipts. To reproduce an old attempt, restore
the corresponding file as `e2e/tpcds/load.py` in an isolated checkout; do not run
it from this evidence directory because native-harness imports are repository
relative. The current loader adds explicit flow control.

Baseline installation identity:
`1df5c77900ed85604f6c35821e0fc767d7ade968814daee0a80359c8485a13eb`.
The one-file row-prefix overlay is
`c707145b075a26682765bddf31dc8833eda03f024c31906575a60f5d41578509`;
`row-prefix-package.json` verifies the base and all changed payload/bytecode hashes.
Production source for that overlay is commit `94474db`.

All four attempts use the immutable qualification container
`sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec`,
CPU affinity 0–7, 16 GiB memory with no swap, no external network, uid/gid 1000,
read-only repository and SF1 data mounts, and a writable second-NVMe report root.
The supervisor deadline is 7,500 seconds; the loader deadline is 7,200 seconds.
Original SF1 input is `eq00-20261006/sf1-02`, with its generation receipt bound
into every load result. The README in `e2e/tpcds` documents the command structure.

`analytical-tests.log.gz` retains all 144 passing worker tests, including both
new complete-transaction row-prefix regressions. `row-prefix-tests.log.gz`
retains the focused 13-test incremental suite. These are correctness checks;
neither log is performance evidence. `harness-tests.log.gz` records the ten
passing input/loader/comparison checks. Original catalog/IAM CI failure logs are
also retained, linked to #181; they are not product load measurements.

`SHA256SUMS` covers every public artifact in this directory except itself.


`reference-02/` retains the independent Apache Spark 4.2.0 execution: all 24
native logical tables / 19,557,335 rows loaded, all 103 statements completed,
11,637 complete result rows. Query rows and plans are gzip-compressed here;
decompress before checking their original per-query hashes or using compare.py.
The reference result records all JVM JAR hashes, Java 17.0.20.1+1 and its Spark
configuration. An initial reference launch failed before starting Python because
the container mount omitted the uv interpreter symlink target; its original log
is retained. No input or SQL was changed to obtain the successful run.

`memory-diagnostic-01` through `04` retain instrumented, isolated investigations
of #182. Attempt 01 could not open a reader lock on a read-only journal; attempt
03 used an invalid parent layout for apply storage. Attempts 02 (planning only)
and 04 (plan/apply) completed. All use the last failed publication and journal;
02 copies the journal, 04 copies both journal and generation. Original state is
mounted read-only. They use an independent 2 GiB container, so completion does
not qualify the 768 MiB product worker ceiling. Samples occur every 50 ms and
can miss short peaks. Scripts and all failed logs remain retained. No diagnostic
rows are counted as product analytical coverage.


`bulk-correctness-02` passes both installed regressions for #178: exact equality
across multiple row-bounded apply batches and preservation of the last good
publication when a single oversized source transaction fails. The first fixture
attempt failed because PostgreSQL needed explicit integer parameter casts for
`generate_series`; its receipt, failure and cleanup are retained. Both attempts
have zero leaked/remaining descendants. This small correctness fixture is not
TPC-DS query coverage or a performance measurement.


`issue-182/` retains the [follow-up memory investigation](../../eq02-apply-memory-investigation.md):
four initial diagnostic pilots (trial 01), a three-repeat/four-variant comparison
(trials 02–04), and one separately profiled run. All 16 diagnostic plan/apply
outputs have identical plans, added rows and unchanged original Parquet bytes.
Candidate predicates and scanner controls are applied only by the diagnostic
script; no product code or limits changed. The initial pilots were taken while
adding experiment branches to that script; the final script was used for all
12 comparison runs. The RSS sampler uses 5 ms intervals and records kernel
high-water RSS as well as Arrow's default-pool counters. `summary.json` binds
the final script and release identity and identifies the pilot/comparison cohorts.

The separate profile is excluded from comparative timings. Its binary pstats
file and text summary are retained. Predicate inspection and exact added-row
verification ran after the timed work. The build download TLS failure from
#177 is also retained here as a CI artifact, not a local workload measurement;
only failed remote jobs were retried. See the runner's mounts for reproduction.
