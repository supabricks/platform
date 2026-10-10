# EQ226 preparation qualification

The read-only cross-batch pipeline is implemented and tested, but remains behind
`sync-lookahead`: installed throughput did not improve. Normal builds stay serial.
The final engineering packages use source `c7af44c`; their proofs record the exact
Cargo feature selection and installed release identities.

| Scope | Serial overall / late rows/s | Preparation overall / late rows/s |
| --- | ---: | ---: |
| 7,385,039 rows | 25,777 / 19,648 (`l2`) | 24,580 / 19,740 (`pa`) |
| 14,770,127 rows | 16,121 / 8,654 (`lgrow`) | 15,370 / 8,689 (`pgrow`) |

Both growing cells and the completed short prefixes pass exact typed checks
across all 24 tables. One run per candidate does not establish a precise effect
size. Earlier M/N/O regressions and failed fixture/setup attempts are retained.
`growing-comparison.json` holds the matched profiles and final correctness status;
`final-disposition.json` records the final tests and explicit non-promotion.
Original SF100 stays paused, SP frozen. No full SF100 or query-suite claim.

Text copies replace repository and evidence-root paths with `<repo>` and
`<evidence-root>`. SHA256SUMS hashes these sanitized archived copies. Hashes inside
receipts bind the original source, package, runner or raw log as stated there;
sanitization can change an archived text file's bytes. Frozen harness revisions
and runner scripts are retained for reconstruction; private runtime state and
credentials are not exported. Component probes use a private cloned journal.

Resource samples are every two seconds. They miss short-lived process CPU tails
and RSS peaks; sampled maxima are not kernel-enforced memory bounds. Consumed
preparation receipts additionally record their process high-water RSS and CPU.
Lag is a sampled commit-to-publication upper bound. Phase medians are nested and
not additive. Throughput excludes input admission and post-run correctness tests;
no builds/tests ran concurrently with either timed growing trial.

[#228](https://github.com/supabricks/platform/issues/228) records the shared growing
worker churn and the remaining retirement-reason/cold-verification attribution.
