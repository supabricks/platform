# EQ02: bounded analytical session resources (#197)

The default analytical worker's resource envelope is too small for nine original
SF1 statements. Raising its memory pool alone does not resolve all nine: q72 still
fails at 2 GiB until its join plan changes. The explicit `analytical` session profile combines a
larger bounded envelope with cost-based join reordering. The existing `compact`
default remains available.

The final managed-session qualification executes all nine formerly memory-blocked
statements and preserves every previously correct result: **97/103 statements
are correct after explicit ordering review, up from 90**. Four alias failures
remain under #198. The diagnostic results below establish the profile choice;
they are not a claim that every statement is correct. Newly exposed q39 DOUBLE
result differences are tracked in [#206](https://github.com/supabricks/platform/issues/206). SP stays frozen.

## Isolating the limits

All probes use the installed Sail source `528b49dac7beafa6d5a0e1f2e538efcbbc4aca2f`,
the retained SF1 epoch, unchanged statements, eight CPUs, and a 16 GiB
container with no swap or external network. Each statement runs in a fresh
process; the parent samples RSS and owned spill files every 100 ms. Plans,
results, schemas, failures, and profile declarations are retained. Completed
results are compared with the existing independent Spark 4.2.0 reference.

| Query pool | Join reordering | Statements completing | Sampled peak worker RSS | Sampled peak spill |
| --- | --- | ---: | ---: | ---: |
| 256 MiB | Disabled | 0/9 | 640 MiB | 70 MiB |
| 1 GiB | Disabled | 8/9 | 1,373 MiB | 88 MiB |
| 2 GiB | Disabled | 8/9 | 2,387 MiB | 0 MiB |
| 256 MiB | Enabled | 5/9 | 480 MiB | 10 MiB |
| 1 GiB | Enabled | 9/9 | 1,590 MiB | 183 MiB |

The compact probes retain the original 256 MiB spill, 1 GiB RSS, and 16 MiB
per-file limits. The 1 GiB pool probes declare 4 GiB total spill, 4 GiB RSS, and
1 GiB per file. The 2 GiB pool probe declares 8 GiB spill and 6 GiB RSS, with the
same 1 GiB file bound. Thread settings and automatic execution parallelism remain
unchanged. These are controlled diagnostic envelopes, not limits changed during
a qualification campaign.

Without join reordering, q72 builds a chain of hash-join intermediates. Its plan
includes repeated `CollectLeft` inputs with severe cardinality underestimates;
the input reservation consumes almost the entire pool at both 1 and 2 GiB.
Reordering resolves q72 at the original 256 MiB pool. It also resolves q21, q39a,
q39b, and q66 there, but q47, q67, q75, and q78 still exhaust memory. The combined
profile resolves those remaining execution failures. Spill files exceed the old
16 MiB file limit, and peak RSS exceeds the old 1 GiB watchdog ceiling.

The first full managed-session run completed the nine originally failing queries
but introduced a q64 memory failure. That 1 GiB candidate is rejected. A follow-up
probe with a 2 GiB pool, 8 GiB spill, and 6 GiB RSS completes all ten statements,
including q64. The replacement full-suite qualification confirms q64 is correct.
A 1 GiB sort-merge preference probe completes only six of those ten statements
and reaches about 1.9 GiB of sampled spill on q72. It is not selected.

| Follow-up profile (original nine plus q64) | Completing | Peak RSS | Peak spill |
| --- | ---: | ---: | ---: |
| 2 GiB pool, join reordering | 10/10 | 2,706 MiB | 84 MiB |
| 1 GiB pool, sort-merge preference, no reordering | 6/10 | 676 MiB | 1,948 MiB |

## Product behavior

Use `analytics open --resource-profile analytical`,
`analytics sql --resource-profile analytical --sql ...`, or
`spark shell --resource-profile analytical`. The API field is `resource_profile`.
Omitting it selects `compact`. A profile cannot be changed on an existing session;
close it and open another. The selected profile and resolved worker limits appear
in session status and epoch metadata.

The analytical profile has a 2 GiB fair query pool, 8 GiB spill budget, 6 GiB
sampled RSS ceiling, and 1 GiB per-file ceiling. It enables Sail cost-based join
reordering. This is a local bounded profile, not automatic sizing for arbitrary
TPC-DS scales or concurrent tenants. The container's 16 GiB hard limit is
separate from Sail's tracked operator pool and the worker's sampled RSS limit.

Admission permits two compact sessions or one analytical session per installation.
The reservation persists through waiting, startup, use, and closing until the
supervisor proves the worker dead. The profile is durable and part of request
idempotency; legacy records deserialize as compact. A capacity rejection happens
before an implicit refresh can be created. Console and notebook entry points
retain their current compact default in this slice.

## Evidence boundary

The native binary and Python session worker are built/collected from a clean
committed platform tree. The engineering package binds both bytes to its build
receipt and checks the worker against that exact Git revision. The retained-load
verifier permits only those explicit platform changes plus the already qualified
Sail artifact; unrelated payload and metadata changes remain rejected. A normal
stopped backup/upgrade applies the package. The original successful load and
independent Spark reference are retained.

The final candidate source commit is `0280d26919ad47b71defc2ea43b23813e5523030`.
Its runtime identity is
`91ec94554e2a14c2afa3ee59025defc9d5afc91a1fa59ea143d667ee09a45e32`.
The baseline runtime identity is
`1208de89e46af8ae20ab62f3378ccf92082072564c53dc792449a70277021cee`.
The rejected first candidate source `8fa1d72d009d9fba7ae35a5580604584d2d98e0b`
and runtime `55337e0cd5805eb07292eb4b4ce1d398700b1e98cde0d4baeba3ccd3719f3cf9`
are retained with their complete result ledger.
This remains unsigned engineering evidence, distinct from release archive CI.

Tests cover durable admission, idempotency, closing/recovery reservations, legacy
records, fixed worker limits, invalid profiles, and rejection of uncommitted or
tampered worker/native artifacts. The existing native library tests and analytical
session tests also pass. The full qualification compares all 24 tables and all
103 original statements, records resource samples, and preserves strict typed
comparison. No numeric tolerance or query rewrite is introduced.

## Final paired qualification

[Summary and hashes](tpcds-evidence/2026-10-08-eq197/summary.json),
[baseline ledger](tpcds-evidence/2026-10-08-eq197/baseline/comparison.json),
[candidate ledger](tpcds-evidence/2026-10-08-eq197/candidate/comparison.json), and
[explicit ordering reviews](tpcds-evidence/2026-10-08-eq197/candidate/review.json)
retain all original statements and exact results.

| Reviewed result | Compact baseline | Analytical candidate |
| --- | ---: | ---: |
| Correct | 90 | 97 |
| Memory execution failures | 9 | 0 |
| Alias execution failures (#198) | 4 | 4 |
| Newly exposed DOUBLE value mismatches (#206) | Not executable | 2 |

Both runs verify exact PostgreSQL/Delta equality for 24 tables and 19,557,335
rows, preserve epoch `fed0899c-c267-4d8a-8432-3e5e920718af` and the original load
receipt, and finish with zero leaked or remaining descendants. The live candidate
also verifies that an active analytical session rejects another compact session.
All 90 previously correct statements remain correct. The newly reviewed q75
ordering case checks identical selected typed rows and identical sorted
`sales_cnt_diff` keys under its original `ORDER BY ... LIMIT 100`.

The same 90 previously correct statements take **104.574 → 108.290 seconds**
(+3.55%) of summed query execution/client time. This single paired
run is descriptive, not a throughput claim. Previously failing statements are
not speedup denominators. The full suite, including table checks and session
lifecycle, takes 466.005 → 469.040 seconds.
Per-query timings and resource samples are retained, including q64's planning
and memory tradeoff.

| Sampled resource maximum across statements | Baseline | Candidate |
| --- | ---: | ---: |
| Worker RSS | 879.6 MiB | 2557.5 MiB |
| Owned spill files | 119.4 MiB | 85.3 MiB |
| Single spill file | 6.1 MiB | 10.6 MiB |

Sampling begins after each worker is ready and runs every approximately 100 ms;
short peaks can be missed. It does not measure the PostgreSQL processes or the
whole container. Query-pool accounting, process RSS, spill limits, and the
container hard limit remain distinct controls.

The rejected 1 GiB full run is retained separately: 96 correct, with q64's new
memory failure. An initial baseline attempt was also rejected because the old
API correctly refuses the new optional field; no TPC-DS statements executed in that attempt.
A regression test now ensures compact qualification requests omit that field.
The corrected baseline and final candidate then ran consecutively under the same
instrumentation and fixed bounds.

Validation: 224 native library tests pass (four existing ignored tests), all 13
existing analytical integration tests and the new capacity/recovery test pass,
seven Python worker tests pass, and 17 TPC-DS harness tests pass. The final worker
is source-bound to the clean committed build; no manual runtime patch or
reference/comparator relaxation is admitted.

The next slice is #198's four alias failures. #206 keeps q39a/q39b unresolved
until their floating-point behavior has its own reduced tests and evidence.
Larger scale, concurrency, and signed-release claims remain open.
