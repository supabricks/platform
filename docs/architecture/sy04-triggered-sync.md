# SY04: triggered incremental synchronization

[Plan](../plans/analytical-sync-implementation.md) · [Workflow](../handbook/triggered-sync.md) · [SY03](sy03-incremental-epochs.md)

Status: implemented for review, 2026-09-22. Local-owner policies support
`mode=triggered, strategy=incremental` through the existing managed-sync API and
CLI. Manual and UTC interval runs share the same durable controller. Continuous
application, event producers, console controls and governed capture remain gated
for later slices. The console capability stays false until its SY06 integration;
the local IPC capability advertises triggered support.

## Enrollment and run boundary

Creating a triggered policy explicitly enrolls an owned SY02 capture generation
and starts its isolated bootstrap. It does not automatically publish a snapshot.
The source must be running and match the qualified whole-table profile. Capture
continues between runs, holding its compute lease, source slot and bounded spool.
One admitted capture and one incremental writer remain the installation limits.
A manual run may queue during bootstrap; its absolute deadline includes that wait.

A run records its policy revision, capture identity, idempotency key and deadline
before requesting a source barrier. After bootstrap verification, the capture
worker emits one transaction containing `pg_logical_emit_message(true, prefix,
run_id)` with synchronous commit enabled. The prefix contains the capture UUID;
the content is the canonical run UUID. The qualified pgoutput decoder accepts
only this owned transactional message in a complete Begin/Commit envelope. Unknown
messages and the existing DDL fence still block the whole group.

The journal atomically persists the barrier's **commit end LSN**, transaction and
captured cursor before acknowledgment. This supplies a target even on an idle
source. The first durable occurrence for a run wins; repeated emission after a
worker crash does not move that receipt. Recovery verifies the receipt against
the checksummed transaction chain. Decoder version 2 is used for new triggered
captures; existing SY02/SY03 manual captures retain their version-1 message profile.

The daemon pins that receipt as `target_lsn` exactly once, before applying changes.
Target acquisition is asynchronous: commits before the barrier are included;
transactions committing after it belong to a later run. A source transaction
already open when the run was requested can commit after the barrier and therefore
be excluded. Admission time is not a database-wide transaction snapshot timestamp.
A queued run with no target yet makes no freshness claim.

## Bounded catch-up and publication

The controller publishes the frozen baseline if necessary, then admits serial
SY03 batches capped at the fixed target. Each batch consumes only complete
transactions and publishes its complete group map and cursor atomically. Larger
runs can expose intermediate complete epochs; success means the **final published
boundary equals the pinned target**, not merely that one batch succeeded. Captured
changes beyond the target stay in the journal for the next run.

Batch admission and the parent run's batch link commit together. A durable owner
ID on each batch lets the publisher recheck policy revision, run state, capture,
target and deadline before publication, including after daemon restart. Direct
`sync apply` is refused for a triggered-owned capture. Readers retain their chosen
epoch. An idle/no-change run advances the barrier boundary using a new descriptor
but reuses table versions and writes no new Parquet files.

There is no second full export after enrollment. A run uses at most 64 batches;
the inherited SY03 limits include 16 MiB complete input per batch, 1 GiB shared
root, 4,096 files, 768 MiB sampled worker RSS, three worker attempts and a five-minute
batch deadline. The policy deadline defaults to five minutes and may be 10 seconds
to 30 minutes; each batch is also capped by the run's remaining deadline. The policy
`max_bytes` controls bootstrap export, not a larger incremental-root allowance.

## Scheduling, pause, cancellation and restore

Schedules retain SY01's 60–2,592,000 second UTC interval and coalesced missed-run
rule. Only one run per source group can be active. Overlapping manual requests
with different keys are rejected; identical retry keys return their original
admission receipt. Missed/overlapping scheduled ticks advance the schedule phase
without building a backlog. A failed scheduled admission records its error and
waits for the next due time. Browser lifetime has no role.

An idle policy pause disables new runs but keeps capture active and bounded.
Resume and schedule edits reuse that enrolled capture; its enrollment identity
stays immutable while each run is fenced by the current policy revision. Pausing,
updating or cancelling a run that owns an unfinished batch cancels the batch and
fences the capture as `resync_required`, since private Delta commits may exist.
Already published complete epochs remain available. If the final target commit
wins a cancellation race, the run remains successful and cannot be undone.

Deleting a policy cancels work and requests owned capture cleanup. Historical
reader references still retain shared analytical roots under SY03's rules. Changing
between snapshot and triggered modes requires deleting the old capture first.
[SY05](sy05-continuous-sync.md) adds checkpoint-preserving transitions between
triggered and continuous modes after active runs drain.
Resync requires explicit capture deletion and enrollment; no automatic full-copy
fallback occurs. Another workflow moving the branch head causes batch admission
to fail rather than silently overwrite that head.

Catalog schema 26 adds the managed batch-owner index and fences older runtimes.
Stopped backup restore preserves a final target publication that committed just
before parent reconciliation, cancels unfinished intent, pauses policies and
fences copied capture generations. It does not replay a copied barrier request
against unrelated source history. The upgrade path also recognizes the additive
incremental-descriptor format declaration while rejecting unknown format versions.

## Evidence and limits

Rust tests cover retries, overlapping runs, fixed targets across restart and
multiple batches, direct-apply rejection, deadline/policy fencing, late cancel,
schedule coalescing, benign pause/resume revisions, capture-generation replacement
and restored publication intent.
Python tests validate message ownership, idle barrier application, duplicate
emission, journal corruption and process exits before/after the barrier/cursor
transaction commits.

`e2e/native/triggered.py` exercises real PostgreSQL, Delta, Sail and daemon restart.
Its scenarios include enrollment, a transaction opened before but committed after
the fixed barrier, writes beyond an already pinned target, old
readers, an idle run, over 16 MiB of captured input split across batches with the
original bootstrap unchanged, missed schedules, pause/resume and cancellation with
owned cleanup. All six scenarios passed locally on Linux on 2026-09-22. The harness is wired
into Linux/macOS native-cell CI; SY04 remote results are pending. Source-level results
are not complete installed-archive qualification or a physical power-loss claim.

The inherited 1,024 incremental-run and 4,096 retry-receipt journal limits are
installation-wide; resync does not clear those histories. Maintenance of these
journals is later work. Spool pruning and per-file Delta vacuum/compaction remain deferred; long-lived
captures eventually need explicit resync at their finite limits. Checksum I/O still
scales with retained files. This slice provides no sustained throughput or lag SLA,
no automatic event trigger and no incremental Unity Catalog publication.

The barrier follows PostgreSQL's [logical message format](https://www.postgresql.org/docs/17/protocol-logicalrep-message-formats.html)
and [logical replication message option](https://www.postgresql.org/docs/17/protocol-logical-replication.html),
qualified here on the shipped native engine.
