# SP10b — Private single-owner journal reads

Status: implementation and qualification in progress. No performance claim or
engine decision. SP10a's accepted SQLite package is the predecessor.

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
supervised `campaign-01`; its final measured review is pending.
