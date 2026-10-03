# SP10b — Private single-owner journal reads

Status: implementation and measurement reviewed, 2026-10-03 UTC. Keep the owner
interface for the SP10c comparison; no speedup or engine-adoption claim. Two
installed release CI failures remain unresolved. SP10a is the predecessor.

## Ownership and authority

Capture retains the exclusive spool lock and its existing write connection,
WAL/FULL durability, append groups, feedback cursor and prune policy. One bounded
service thread in that process opens read snapshots through the backend contract.
An incremental worker receives bounded materialized ranges over an AF_UNIX socket;
it does not open SQLite on this production path. There is no TCP listener or
runtime backend selector. Direct reads remain available to isolated backend tests
and the predecessor measurement, with no fallback from failed owner requests.

The socket is `tmp/<capture-id>/journal.sock` under the private cell root. Its
parent is mode 0700 and the socket is 0600. Startup holds the exclusive spool
owner lock, refuses live/foreign/non-socket paths and reclaims a stale socket.
Shutdown joins the reader before closing storage. Restart never replaces the
spool or changes feedback authority.

The daemon issues a private apply input only after validating run, publication,
project, source and policy authority. A read includes the exact run/attempt/epoch,
identity (including service authority), worker generation, bootstrap, immutable
LSN bounds, deadline and endpoint/source/policy scope. The owner compares it with
that daemon-issued input, checks current capture control, and repeats those checks
before sending bytes and before the final completion marker. UUID-derived paths
cannot escape the owned apply-work directory. Pause, deletion, generation/source/
policy changes, replaced attempts and removed grants fence reads. The native
publisher retains its independent authority checks before accepting results.

Same-UID code that can modify private daemon files remains inside the existing
local-owner trust boundary. This transport is not an isolation boundary between
arbitrary programs running as that OS user.

## Bounds and failure semantics

- One service thread and active reader; no application queue; socket listen backlog
  one (the operating system controls exact backlog admission). The original
  monotonic deadline includes queueing, connection, request, read and response.
- Three seconds per read, capped by the issued apply deadline. Existing 32-attempt
  pre-apply retry accounting shares that deadline; retries never refresh targets.
- Requests: 32 KiB. Schema/response header: 2 MiB plus 4 KiB framing metadata.
  Payloads: 4 MiB per transaction, 16 MiB per range, at most 65,536 transactions.
  Complete response: at most 24 MiB including record framing and checksums.
- The existing snapshot metadata budgets and identity, contiguous-history,
  checksum, bootstrap and pruned-prefix checks run inside the owner. It closes
  the snapshot before serialization or socket writes. Slow readers retain only
  bounded materialized memory, with no persistent snapshot handle or database pin.
- Reads check cancellation while scanning/encoding; SQLite's progress handler
  also checks cancellation/deadline. Socket operations poll at most 100 ms between
  checks. Disconnect, expiry or owner shutdown releases readers. Storage writes
  and durable feedback remain on the original capture thread.
- Clients require request binding, bounded frames, ordered cuts, matching payload
  hashes/byte totals and a final fenced completion marker. Partial data never
  escapes the read function. Owner unavailability, incomplete connections and
  timeouts can use the existing read-only deferral path; invalid authority,
  protocol and storage corruption remain fatal. No retry crosses Delta mutation.

The persisted SQLite schema and dependencies are unchanged. The native change
adds the same endpoint/source/policy scope to capture control and apply input.
The capture backend supplies snapshots from its process; no applier may use its
write connection.

## Validation and measurements

Tests cover parity, exact grants, cross-project/generation/authority rejection,
pause/policy/attempt revocation, corrupt records, malformed/oversized/incomplete
responses, bounded materialization, cancellation, slow consumers without reader
pins, missing-owner deferral, process kill/stale-socket restart and revocation
after payload bytes but before completion. Existing replay, crash, durable-group,
publication, retention and worker-reuse tests remain required. Installed lifecycle
fixtures must also retain pause/restart/schema and atomic pinned epochs.

Freeze source, packages, controller, unchanged SP06 source workload, native
profiling feature, image and configuration. Only owner-related native/Python
files change from accepted SP10a; dependencies and profiler stay byte-identical.
Run source tests, installed manifest/diagnostic checks and functional screens
before admitting measurements.

The campaign retains 108 fresh fixtures, sequentially: six read components, six
full lifecycle fixtures, 24 observer-off/on controls, 24 historical comparisons,
12 qualified comparisons and 36 final profiler-off/on controls. Use the same
whole-SMT 4/8/16-CPU cells, 16 GiB/no swap/no quota, 64 GiB free-space admission,
five-minute quiet interval and bounded whole-pair contention replacement policy.
Historical trials use four clients, five-second warmup and 45-second load;
qualified trials use eight clients, 1,250 offered rows/s, 60-second warmup and
300-second load. Preserve source misses and runtime failures for investigation.

The new read component replaces SP10a's write abstraction component. Both arms
have a separate durable writer process and identical 4,096 × 1 KiB records.
Read 32/512/4,096-record ranges, three warmups and 16 measured requests per size,
with three fresh matched predecessor/candidate pairs. Record end-to-end time,
client plus owner CPU, response bytes, backend read/validation time and payload
encoding/hash time. Header parsing/encoding and transport remain inside the
end-to-end measurement; do not subtract independent percentiles to attribute
causal shares. Requests inside a fixture are correlated, not independent trials.
Prove that new appends cannot move a frozen target. These rates are not pipeline
capacity. Original raw write/replay tests still verify durability separately.

Acceptance requires correctness, cleanup, input and freshness. Investigate paired
median source loss >5%, p95 growth >5%, or CPU/peak-memory growth >10% in any main
cell. Report component and activation costs even below the screens. This slice
measures the cost of ownership needed for a fair SP10c comparison; it does not
justify RocksDB by itself. SP10c must compare identical owner interfaces and then
total cost against accepted direct SQLite. SP11 sustained/rotation/reader-pressure
and SP12 release gates remain outstanding.


## Initial installed screen finding

Candidate `b8ce4f2` passes the installed owner read component, but the first
lifecycle apply is rejected with `invalid_journal_read_accounting`. Detailed IPC
counters had been added to the native daemon's closed five-field journal receipt
schema. [Issue #146](https://github.com/supabricks/platform/issues/146) tracks the
finding. Keep that failed smoke and immutable package; no measured trial was
admitted. The correction separates benchmark transport counters from operational
receipts and adds an exact-key regression test. A fresh installed screen must pass
before freezing the measured candidate.


## Corrected installed screening checkpoint

Source `a37bf26` (native `b8ce4f2`) passes all four installed screens. The same
lifecycle reuses one process across successive epochs, recovers an in-flight
worker kill, retires idle workers, and passes pause/restart/schema and atomic/pinned
checks. Observer off/on smokes converge to both frozen source tables. The exact
predecessor also passes the read component. Every successful screen cleans up
with zero leaked/remaining descendants.

All 119 current analytics tests, 334 native tests (four ignored), and 76 harness
tests pass. The new queue-pressure test exercises production retries after the
socket backlog drains; expired queued clients open no snapshots. [Screen receipts,
initial failure and package proofs](sync-performance-evidence/2026-10-02-sp10b-screen/README.md)
are retained. These are functional screens, not measured performance results.
`candidate-runtime-02`, `harness-02` and `config-02.json` are frozen for the new
supervised `campaign-01`; its final measured review follows.


## Final measured review

All 108 fixtures completed: six read components, six lifecycle, 24 observer
controls, 36 main comparisons and 36 profiler controls. There were no failed
trials, replacements or cleanup leaks. Raw hashes and metrics were reconstructed
with the frozen controller; all main cells pass the predeclared regression screens.
[Individual results, receipts, reconstruction script and decision](sync-performance-evidence/2026-10-03-sp10b-reviewed/README.md)
are retained. The campaign finished 2026-10-02; review completed 2026-10-03 UTC.

| Logical CPUs / offered rows/s | Actual source, direct → owner | p95 lag, direct → owner | Paired p95 change | Paired CPU change | Paired peak-memory change |
| --- | --- | --- | --- | --- | --- |
| 4 / 50 | 50.032 → 50.035 | 1.902 → 1.923 s | +1.70% | −0.94% | −0.57% |
| 16 / 50 | 50.040 → 50.039 | 1.966 → 1.944 s | +1.20% | +0.92% | −0.59% |
| 8 / 1,000 | 728.304 → 734.262 | 2.230 → 2.219 s | −0.72% | +1.62% | −0.63% |
| 16 / 1,000 | 749.150 → 743.644 | 2.248 → 2.229 s | −1.13% | +1.24% | +0.67% |
| 8 / 1,250 qualified | 1,249.391 → 1,249.474 | 3.002 → 3.002 s | +0.98% | +1.28% | −0.41% |
| 16 / 1,250 qualified | 1,249.791 → 1,249.867 | 3.025 → 3.015 s | −0.15% | +1.12% | −0.02% |

Values are medians of three fresh trials; changes are medians of paired changes,
which can differ in sign from a change between medians. Every main trial is correct
and below five-second p95. All 12 qualified trials attain input. The historical
four-client overload is source-limited and does not qualify 1,000 rows/s. These
short profiles establish neither statistical equivalence nor sustained capacity.

### Read component cost

| Range (1 KiB records) | Round trip, direct → owner | Paired change | Client + owner CPU for 16 reads, direct → owner |
| --- | --- | --- | --- |
| 32 / 32 KiB | 0.363 → 1.110 ms | +206.19% | 0.0060 → 0.0203 s |
| 512 / 512 KiB | 1.503 → 7.996 ms | +431.96% | 0.0257 → 0.1333 s |
| 4,096 / 4 MiB | 15.401 → 56.417 ms | +266.33% | 0.2570 → 0.9382 s |

This is a material ownership/transport cost despite modest pipeline effects in
the reference workload. At 4 MiB, separately measured owner read/validation and
payload encoding/hash medians are 21.49 and 10.42 ms. Header processing, grants,
framing, communication and client validation also contribute to the end-to-end
measurement; independent timing distributions cannot be subtracted to isolate a
causal transport share. All read ranges are exact, remain bounded and retain
their target across append. No throughput gain is inferred from these opaque records.
[Issue #147](https://github.com/supabricks/platform/issues/147) preserves the cost
and potential buffering/framing/cancellation-check follow-ups. Any optimization
must be a separately measured variant, not a replacement of this frozen evidence.

### Lifecycle and instrumentation controls

All six lifecycle fixtures pass reuse, kill recovery, idle retirement, pause/restart,
schema fences and atomic/pinned epochs. Median idle CPU is 0.0893 → 0.0884 cores;
API p95 is 43.791 → 38.272 ms. These auxiliary samples do not establish an API SLA.

Profiler activation on the candidate adds 7.41–9.75% paired CPU across historical
cells and 8.02% / 9.01% at qualified 8/16 CPUs. Qualified p95 changes +0.09% /
−0.78%, and peak memory +6.67% / +5.41%. Historical profiler-control p95 changes
span −0.91% to +1.94%. The unchanged instrumentation has a cost; these results do
not close the earlier overhead findings in #144 or permit subtracting that cost
from the runtime comparison.

Observer off/on controls add 1.60% / 1.91% CPU on direct SQLite and 1.59% / 1.43%
on the owner at 8/16 CPUs. All 24 controls attain approximately 1,250 rows/s and
independently match both frozen source tables. Drain medians span 1.72–2.75 s;
they depend on final epoch phase and cannot qualify p95 freshness.

### CI and decision

On source/evidence head `aabbb16`, all six protected checks and both installed
sync gates pass; 44 checks pass overall. Two installed release jobs fail:

- Linux governed data returns `conflict` at the post-restore fresh-token SQL
  request (`qualify.py:260 → request:58 → cell.py:93`). Identity passes; cleanup
  leaves zero descendants/containers. The signature recurs in [#110](https://github.com/supabricks/platform/issues/110);
  the underlying conflict and relationship to SP10b remain unestablished.
- macOS catalog service raises `PermissionError` while inspecting the private
  JRE command line (`service.py:156`). Cleanup succeeds with zero descendants;
  browser/recovery do not run. [#148](https://github.com/supabricks/platform/issues/148)
  tracks this distinct call site, separate from #140's descendant census failure.

The [CI evidence summary](sync-performance-evidence/2026-10-03-sp10b-reviewed/ci-review.json)
retains exact jobs, original artifact hashes and sanitized diagnostics. No failure
is discarded or declared fixed by a later rerun. Final documentation commits
trigger fresh checks; this report identifies the tested source head explicitly.

Decision: retain the measured owner interface for a fair SP10c engine experiment.
The slice meets correctness/freshness/input and main regression screens while
paying an explicit IPC cost; it delivers no performance improvement by itself.
SP10c must use this same ownership/transport for both SQLite and RocksDB, then
compare total cost against the best direct SQLite package. No RocksDB adoption,
full release qualification, SP11 soak/pressure result or SP12 completion is implied.
