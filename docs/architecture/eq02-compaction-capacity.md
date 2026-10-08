# EQ02: compaction capacity and retained append histories

Status: candidate fixes for [#193](https://github.com/supabricks/platform/issues/193),
[#194](https://github.com/supabricks/platform/issues/194), and
[#195](https://github.com/supabricks/platform/issues/195) pass targeted qualification and the full SF1 load.
[Retained evidence](tpcds-evidence/2026-10-07-eq02/compaction-capacity/summary.json).
SF1 attempt 11 publishes all 19,557,335 rows in 4,431.262 seconds. Exact table
verification now passes after the #196 restart fix. [All 103 product statements](eq02-sf1-results.md)
were attempted; 69 are correct after review and 34 have tracked failures/mismatches.
SP stays frozen. These changes preserve the original memory, storage, input,
and deadline limits.

## Actual output versus conservative admission (#193)

SF1 attempt 10 stops after 3,434.826 seconds at 17,194,051 committed and
17,128,515 published rows. Twenty-three tables finish loading; the final
`store_sales` table has 517,120 acknowledged rows. The compaction admission
estimate doubles the active Parquet bytes and adds 4 MiB. That estimate exceeds
1 GiB for a 537,961,844-byte generation, despite the actual output fitting.
The host is not out of space. Capture fences with `incremental_disk_budget`;
the previous publication remains intact, and cleanup leaves zero descendants.

Commit `dd455da` caps the initial reservation at the remaining generation quota.
Streaming live-file checks, retained-generation admission, free-space checks,
and final output checks still enforce actual limits. Proven appends reserve
source work without another target-sized merge reservation. The manifest keeps
the first published size of each generation; byte-triggered rollover occurs
halfway through its remaining 1 GiB capacity instead of immediately repeating
when a compacted baseline already exceeds 512 MiB. Legacy manifests keep their
original conservative threshold.

A fresh replay of the retained failed batch reproduces the predecessor rejection.
The capacity candidate completes in 91.772 seconds at 450,891,776 bytes peak RSS.
It compacts 17,128,515 rows and applies the saved 5,120-row prefix. Output is
550,372,983 bytes. These are diagnostic worker measurements, not end-to-end
throughput; the individual worker kill is disabled for phase attribution.
The unchanged 768 MiB production limit is still the acceptance threshold.

The correction passes 155 worker tests, including compressed input whose actual
output exceeds a smaller test quota and must fail without altering the old root.
Native validation includes 53 sync-state and 13 publication tests.

## Append-only generations (#194)

Attempt 10 retains all 1,200 publication receipts. They record 15 whole-generation
compactions taking 586.227 seconds; 513 MiB of current data leaves 2.49 GB of
retained generations. Every apply is an append and removes no prior Parquet file.
Compaction therefore repeatedly copies a growing live set.

Commit `a425278` marks a generation append-only only while every apply has the
saved-plan proof. A merge clears that marker permanently for that generation;
missing legacy markers are conservative. Such proven histories roll over at
512 versions. Mixed or legacy histories retain the 64-version trigger. The hard
1,024-version limit, 2,048-file rollover, 4,096-file hard limit, byte triggers,
and 4 GiB retained-root limit remain unchanged. No historical root is deleted.

All 155 worker tests and four native boundary tests pass. The installed history
fixture reaches version 512, checks every value in versions 0, 1 and 512, and
peaks at 417,079,296 bytes RSS. Its 511 individual append commits take a median
13.59 ms, p95 22.18 ms, and maximum 47.57 ms. This fixture validates bounded log
history and retained readers; it is not PostgreSQL sync throughput. Full-load
attempt 11 retains all 1,373 publication receipts and records two compactions
totaling 161.319 seconds. Attempt 10 stopped earlier, so the two attempts do not
establish a controlled throughput speedup.

## Wide-value writer buffers (#195)

The new capacity fixture contains 65,536 rows with unique 8 KiB text payloads:
538,672,426 bytes across 32 Parquet files. It compacts that table and appends
16,384 new keys. The capacity-only candidate fits disk but peaks at
1,300,054,016 bytes, exceeding the worker budget. Fresh phase profiling localizes
the spike to the compaction write, before planning or apply.

Scanner-only traversal peaks at 279,326,720 bytes. Reducing the writer queue to
one batch alone still peaks at 1,284,227,072 bytes on eight CPUs. A diagnostic
one-CPU/one-batch run peaks at 385,892,352 bytes. These controls implicate parallel
writer buffering; limiting just the source scanner is insufficient.

The reviewed Delta source patch adds opt-in write memory/spill settings. Bounded
writes use one execution partition and 256-row batches. Compaction requests the
existing 64 MiB pool and 64 MiB spill allowance. Other writes retain their default
configuration. The pool is not a total RSS bound; the daemon still enforces RSS.
The source pin, patch hashes, native artifact verification, and Linux/macOS
archive gates bind this correction to the installed runtime. The source-based
native-cell workflow also installs the verified custom wheel rather than the
upstream wheel that lacks this API.

| Wide-table fixture | Capacity-only predecessor | Bounded write candidate |
| --- | ---: | ---: |
| Compaction and append elapsed | 2.407 s | 2.235 s |
| Peak RSS | 1,300,054,016 B | 469,741,568 B |
| Final generation | 539,005,851 B | 539,005,851 B |
| Within original 768 MiB limit | No | Yes |

The candidate faults after the Delta commit, recovers the saved plan without a
second commit, verifies exact old/new rows and old source hashes, then checks the
512-version history. Replay peaks at 189,472,768 bytes. All three installed
capacity checks pass with zero leaked or remaining descendants. These are single
controlled fixture runs, not a claim of statistically established speedup.

## Observation and remaining gates

Attempt 10's original external sampler cached negative process roles across
fork/exec (#191), so its peak is only an observed lower bound. A supplemental
observer started about 1,050 seconds into the unchanged run; both observers saw
522,997,760 bytes peak. The supplemental observer adds 181.27 CPU seconds and
records eight process-exit errors. The corrected maintained sampler is covered
by a real same-PID fork/exec regression and ran from the start of attempt 11.
It observed 134 apply workers, a 525,434,880-byte peak and zero sampling errors;
sampling can still miss transient peaks.
Process-owned fixture and replay high-water measurements above are unaffected.

The bounded-write candidate passes all 155 worker tests, 75 packaging tests,
and 11 TPC-DS harness tests. All 34 installed checks across ten suites pass with
zero leaked descendants. The final retained-batch replay passes in 94.405 seconds
at 405,692,416 bytes peak RSS; this is a memory reduction, not a replay speedup.

Attempt 11 completes the original-bound load and drains all 19,557,335 rows in
4,431.262 seconds (73.9 minutes; about 4,413 rows/s including startup/cleanup).
Its final generation is 761,416,771 bytes. Cleanup leaves zero descendants.
No load limits were raised and no active measurement was restarted.

The following verification restart fails before any table or SQL check (#196):
synchronous retained-publication recovery delays authorization of storage
children beyond their readiness-probe window. The candidate finishes recovery
before launching those children. Verification resumes on a retained copy through
an explicit stopped native-only upgrade with its backup and both release identities
recorded; the successful load receipt remains unchanged. All 24 exact table checks
pass and all 103 statements are attempted: [69 correct after review, 34 remaining
failures/mismatches](eq02-sf1-results.md). Cleanup leaves zero descendants.

CI on `9ee327b` passes 33 jobs, including both Linux/macOS release-sync gates.
The macOS notebook recovery job fails after 19 successful checks (tracked in #136).
Complete query correctness and exact release-archive qualification remain open.
