# SP11 sustained lookup correction — issue #169

Launch 03 passed all six 15-minute fixtures but stopped after its 8-CPU one-hour
fixture. The final 16-CPU one-hour trial did not run. Original receipt hashes
were verified; summaries and the full failed timing/resource evidence are
preserved here. Actual input averaged 899.356 changed rows/s against 1,250 offered;
overall p95 was 6,670.504 ms and worst-window p95 8,844.416 ms. The first failing
five-minute window spans minutes 21–26. Data equality, drain, memory/spool/backlog
screens and cleanup passed. There were no detected build overlaps. This remains
failed evidence, not a successful qualification.

## Logical slice: index retained generation references

`Cell::control_incremental` checks whether each storage generation is still
referenced. The existing query scans retained snapshots, joins each publication
and parses its descriptor to find a generation. With more retained generations,
it repeats that history scan on every daemon tick. The generation rotates after
64 table versions, so this work grows with sustained publication history.

Catalog migration **31** adds `publication_storage_generation` on
`json_extract(descriptor,'$.generation'), export_id`. The query and its snapshot
states, reader/pin retention, active-writer source/destination checks and deletion
rules are unchanged. The planner can find a generation first and then perform
indexed snapshot lookups. Backup/restore format checks, release declarations and
explicit predecessor migration tests advance with the schema.

A native regression using 2,048 valid retained publications measures **26,649 ->
23 SQLite VM steps** for the same missing-generation query. It also verifies
index updates and available/unavailable/deleting/deleted snapshot behavior;
existing tests cover pinned readers and active writers. This is deterministic
query-work evidence, not a whole-stack speedup claim. A copied live catalog also
showed about 3.9 -> 0.04 ms per lookup cycle; that exploratory timing alone is not
an accepted performance comparison.

This is a demonstrated growing cleanup cost. It does not yet prove that this
single change explains the entire source-throughput/I/O slowdown. The long
qualification must establish that separately; thresholds will not change.

## Diagnostic disposition

An exploratory profile was stopped after collecting query-plan evidence and the
native regression. Its later portion overlapped local builds, so it is excluded
from throughput/latency qualification and causal claims about disk latency.
`diagnostic-limitations.json` records the interval and zero-leak cleanup.
The partial private fixture is retained locally. Its state database contains
private fixture data and is deliberately not exported.

## Validation and fresh campaign

All 53 targeted sync tests pass, including compaction/pin protection and the new
query regression. The broader local unit run passes 216 tests (four pre-existing
ignored). Recovery validation checks the explicit backup/restore schema-31 and source-30
upgrade allowlists. Initial failures exposed these required format declarations;
the corrections must pass before assembling a candidate and launching trials.
Installed validation is recorded below once complete.

The new runtime will be a performance overlay changing only the native binary
and declared local-catalog format, on the unchanged #157 package. The qualifier,
load, memory policy v2 and all other gates remain unchanged. The eight-trial
campaign will start with the **8-CPU one-hour fixture**, then the six alternating
15-minute repeats, then the 16-CPU one-hour fixture (210 measured minutes total).
This order exercises the known failure first. Other SP11 phases remain pending.

### Completed validation and launch 04

The corrected source-30 upgrade test passes, covering every upgrade boundary in
its fixture; 31 existing recovery cases had passed the preceding run. The release
build, 216 local unit tests, 100 performance harness tests, eight release-evidence
checks, three sync-evidence checks and format-constant check pass. Exact test
runs, retained failures and log hashes are recorded in `validation.json`.

The 60-second installed screen (after 60-second warmup) passes all individual
gates at **1,248.270 changed rows/s / 2,658.263 ms p95**, with exact equality,
foreign keys, journal reopen and zero leaked/remaining descendants. Its windows
reproduce exactly with the frozen analyzer. This is plumbing validation, not a
sustained capacity result or a statistically qualified latency improvement.

**Launch 04 stopped on throughput**, using frozen source `2207494e053af74c57da863991dc1e9d76c93591`
and runtime identity `d03e01db77563e1da1d4b1b0da992727e7cc89196212b1eb91173ff700421bb7`.
The config, package proof and launch snapshot are archived here. All 26,038 shared
payload files match the predecessor; only the native binary and catalog format
change. This is an unsigned performance overlay, not a newly assembled full
release. Live status: `build/issue169-20261006/steady-04/status.json`.

The first fixture is the previously failing 8-CPU one-hour profile. Memory policy
v2 and throughput/lag/correctness gates are unchanged; failures stop the campaign.
Eight fixtures contain 210 minutes of load. Qualification and #169 confirmation
remain pending. No original result is replaced or reclassified.

### Launch 04 review

The first 8-CPU one-hour fixture completed at **800.951 changed rows/s**. Its
committed and published throughput windows fail the unchanged 1,000 rows/s gate;
the first failing window spans minutes **13–18**. Overall freshness p95 is
4,232.039 ms and worst-window p95 is 4,816.587 ms, so all freshness windows pass.
Data equality, bounded drain (2.483 seconds), memory, backlog, spool, inspection
and cleanup gates pass. Zero leaked or remaining descendants are recorded.
**0/8 accepted; one executed; seven not run.** The controller stopped automatically.

[Raw evidence and deterministic replay](launch04/review.json) preserve this
failure. Every window reproduces with the frozen analyzer. The index's proven
query-work reduction did not qualify sustained throughput. These unpaired runs
do not establish a causal end-to-end speedup. Source COMMIT/storage diagnostics
are required before another runtime intervention; no gates have been relaxed.

### Source commit/storage diagnostic

The fresh 25-minute [source diagnostic](source-probe/diagnostic-summary.json) uses
the same indexed runtime, eight clients, 1,250 offered rows/s, and the production
transaction/durability settings. It adds bounded minute SQL timing totals,
PostgreSQL wait/table/WAL observations, safekeeper metrics, and host/device IO.
It is diagnostic evidence, not qualification or an accepted profiler comparison.

It reproduces the source slowdown after about 19 minutes: full minute source
transaction counts fall from about 37,000 to 19,500 (about 1,230 to 650 changed
rows/s), and mean COMMIT rises from about 12 to 23 ms. Safekeeper flush mean rises
from about 4.1 to 7.9 ms, while flush completions fall from about 194/s to 99/s.
Host NVMe flush completion counters similarly rise from about 2.8 to 5.4 ms per
flush. These are concurrent observations, not proof of SSD hardware failure or
of a specific competing process. Host write-time counters have discontinuities
and remain excluded; many host processes are unreadable. Temperature and readable
process IO observations start about eight minutes into the run.

The full run averages 1,073.317 rows/s with 3,598.275 ms overall publication p95;
its late throughput windows fail. Exact final equality, journal reopen, foreign
keys and zero-leak cleanup pass. No build overlap was recorded. No source fix
or speedup is claimed. The next controlled diagnostic changes only the mutable
fixture's location from the system NVMe (`/`) to the second NVMe (`/data2`).
It preserves the original device's failures and does not retroactively qualify
that storage profile.
