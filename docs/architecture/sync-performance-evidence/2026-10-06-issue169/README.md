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
ignored). Recovery and installed validation are in progress. An initial recovery
run exposed the explicit schema allowlist needing version 31; that was corrected
before assembling a candidate. The rerun must pass before launch.

The new runtime will be a performance overlay changing only the native binary
and declared local-catalog format, on the unchanged #157 package. The qualifier,
load, memory policy v2 and all other gates remain unchanged. The eight-trial
campaign will start with the **8-CPU one-hour fixture**, then the six alternating
15-minute repeats, then the 16-CPU one-hour fixture (210 measured minutes total).
This order exercises the known failure first. Other SP11 phases remain pending.
