# SP03b capture WAL qualification and attribution

Status: implementation and local fault checks in progress. Performance and
installed Linux/macOS qualification are pending. SP03a merged in PR #109.
This document declares the experiment before measurements start.

Only the capture spool changes to WAL/FULL. PostgreSQL, Delta and the publication
catalog keep their existing roles. Production grouping stays at 32 complete
transactions, 1 MiB or 10 ms. The fixed SQLite dependencies qualified by SP03a
remain unchanged. No 1,000-row/s capacity or latency improvement is claimed yet.

## Durability, ownership and physical bounds

The worker checks the returned journal mode and FULL setting. It disables
automatic checkpoints and checkpoint-on-close; the owner-locked capture writer
performs explicit checkpoints. A PASSIVE checkpoint runs at most once per second
when the WAL reaches min(4 MiB, spool limit / 16). A zero-wait TRUNCATE is attempted
when write headroom is insufficient, and before migration back to DELETE. A busy
reader retains its snapshot and causes capture backpressure. These are initial
bounded settings, not an SP05 checkpoint tuning experiment.

The configured 16–512 MiB spool budget covers database, WAL, shared memory and
rollback journal. Temporary SQLite storage stays in memory and cache spilling is
disabled. The database page cap is floor((limit − 1 MiB) / (3 × 4 KiB)). Before each
mutation the writer reserves the complete capped database's WAL frame image,
checkpoint growth and 1 MiB for shared memory, in addition to existing sidecars.
It retains the 64 MiB filesystem reserve. This deliberately makes usable row
capacity smaller than the physical limit; a 512 MiB limit is not 512 MiB of rows.
Append admission also retains 256 KiB of database metadata/pruning headroom and
conservative per-transaction page overhead. Every mutation checks the resulting
physical size. No sidecar is deleted to make space.

When pressure prevents group commit, capture stops receiving, retains the bounded
pending group, checks control/fencing and source health, retries published-prefix
pruning, and acknowledges only its existing durable cursor. Pause/stop leaves the
source slot intact for replay after restart. Status exposes physical bytes,
checkpoint count/time/frames/busy outcomes, and pressure. The supervisor preserves
the fixed pressure/migration error codes without exposing source values.

All database/sidecar files must be private, owned, regular and singly linked.
Apply opens mode=ro, holds a shared migration lease only while materializing a
bounded transaction snapshot, and closes cursors/connection/lease before Delta
work. Live databases never use immutable=1. Existing SQLite readers that do not
use the lease still block incompatible journal-mode transitions in SQLite.

Migration holds the exclusive writer lock, verifies identity and the durable
chain, then requires an exclusive reader lease before changing mode. An existing
spool above the new page cap is left unavailable with spool_migration_budget;
it must be drained/pruned under the previous release or moved to a larger supported
physical budget before retry. A migration-busy error preserves the journal and
source slot. Do not deploy the previous release directly over a live WAL spool.
For rollback, stop the stack, use the qualified SP03b Spool with journal_mode='delete'
and the existing identity/limit, and require its successful checkpoint and mode
transition before starting the older release. Never copy only the main database
or manually remove sidecars. Stopped backups include nested capture DB/WAL/SHM;
restore retains the existing explicit capture-resync fence.

The fault tests cover committed WAL recovery, before/after migration/checkpoint
process death, group/feedback/prune crash boundaries, pinned-reader pressure,
read-only access, unsafe sidecars, stopped copy and SQLite-mediated downgrade.
They establish process-crash behavior, not physical power-loss qualification.
Installed release qualification executes the WAL tests with the packaged Python
and capture implementation, with a receipt tied to release identity and test hash.
The dependency probe remains a separate DELETE scratch-database check.

[SQLite WAL and FULL behavior](https://www.sqlite.org/wal.html),
[checkpoint semantics](https://www.sqlite.org/pragma.html#pragma_wal_checkpoint),
[cache spill control](https://www.sqlite.org/pragma.html#pragma_cache_spill).

## Frozen measurement protocol

Freeze one clean source/harness revision. Create immutable diagnostic overlays on
the accepted SP03a package; retain manifests, changed-file hashes and native binary
identity. Both main arms receive the identical updated profiler. It separates
explicit SQLite WAL_CHECKPOINT calls and their native sync counters from COMMIT;
no SQL text, rows or credentials enter traces. The common trial observer also
retains fixed checkpoint/physical-size/group status counters in its existing
backlog samples. The predecessor runtime remains
SP03a. The candidate includes the SP03b worker and native status handling.

Run 30 component trials first: batching on/off × DELETE/WAL × saturated/500 offered
transactions/s, three shuffled repeats each (24 trials), plus three additional
profiling-off/on pairs for grouped WAL at saturated input (6 trials). All use the
SP03b capture implementation, FULL durability, 10 seconds, real pruning and a
bounded read-only reader. Disabled-observer controls still verify the entire
retained chain and pruned-prefix count after the writer stops. These synthetic
rates exclude source commits, apply and publication.

Run 72 full-stack trials sequentially:

- Eighteen SP03a instrumentation off/on controls: three pairs each at 4 CPUs / 50
  rows/s, 8 CPUs / 1,000 rows/s and 16 CPUs / 1,000 rows/s.
- Eighteen equivalent candidate instrumentation off/on controls.
- Twelve fresh SP03a and twelve candidate trials across the mandatory four cells:
  4/16 CPUs at 50 rows/s, 8/16 CPUs at 1,000 rows/s, three repeats each.
- Twelve end-to-end factorial ablation trials at 16 CPUs / 1,000 offered rows/s:
  three pairs of ungrouped/grouped DELETE and three pairs of ungrouped/grouped WAL.
  These use otherwise identical SP03b code and are explicitly diagnostic variants,
  not new production group or checkpoint settings.

Keep four clients, two 10,000-row tables, two row changes per transaction, 5-second
baseline, 5-second warmup plus catchup, 45-second load, 120-second drain, 16 GiB,
no swap/quota, full SMT sibling sets, seeds and observer behavior unchanged.
Require five quiet minutes before each trial; retain host observations and every
failed or contended attempt. Repeat contended pairs without modifying other work.
Runtime failures remain outcomes. Cleanup failures stop the series.

The main matrix measures the complete slice over batching alone. The component
factorial and end-to-end ablations distinguish grouping/WAL interactions from an
assumed additive gain, while controls measure profiling activation overhead.
Investigate paired median latency, CPU or memory regressions above 10% and every
freshness miss. Retain checkpoint/busy/physical-size counters, durable versus
feedback cursors, native sync costs and source-achieved rates. A source-limited
four-client result cannot qualify sustained 1,000 rows/s. Record a keep/revise/revert
decision only after the full evidence and both installed platform gates complete.
