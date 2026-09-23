# SY05: supervised continuous synchronization

[Plan](../plans/analytical-sync-implementation.md) · [Workflow](../handbook/continuous-sync.md) · [SY04](sy04-triggered-sync.md)

Status: [merged in #83](https://github.com/supabricks/platform/pull/83), 2026-09-23 UTC. Local-owner CLI/IPC policies admit
`mode=continuous, strategy=incremental`. Console/governed integration remains
SY06, storage maintenance SY07, and installed-release qualification SY08.

Current integration: [SY06](sy06-sync-surfaces.md) adds shared console/agent
controls, governed service authority and immutable catalog epoch views. The
phase boundary above records what this earlier slice delivered.

## Continuous execution

Enrollment uses the same qualified source, owned capture and isolated frozen
bootstrap as SY04. The daemon automatically admits one bounded catch-up run when
there are captured row changes beyond the published cursor. Each run samples a
fixed captured commit boundary, then uses SY03's serialized, atomic group batches.
Changes beyond that cut wait for the next run. There is no growing timer queue,
row-by-row publication or per-table latest-version read path. Readers remain pinned.

The default desired freshness is 5,000 ms. The default minimum interval between
run admissions is 500 ms; time spent applying counts toward that interval. It is
not an extra sleep after every publication. Configuration permits 1,000–300,000 ms
freshness and 200–60,000 ms intervals, with the interval no greater than freshness.
Continuous policies reject schedules and manual Run now. Policy timeout bounds
each catch-up run, including its serial batches; it does not expire the policy.
Bootstrap retains its separate bounded export lifecycle.

One capture and one materializer per installation remain the admission limits.
The same input, worker, temporary-space, root and transaction quotas apply. On
pressure, history loss or schema failure, the controller preserves the last
complete epoch and stops or fences work. It never skips a prefix or silently
rebuilds a baseline. A failed run stops automatic admission until explicit recovery;
transient source/capture outages can reconnect while retained history is valid.

## Observations and idle sources

A keepalive proves transport liveness, not that logical decoding has consumed all
source commits. Continuous capture therefore emits an owned transactional source
barrier about once per second. The SY04 journal persists that barrier with its
complete transaction before acknowledgment. A recent committed barrier, a fresh
stream observation and the absence of unpublished row changes establish an
observed idle boundary. These receipts do not create analytical epochs when no
row data changed. They still consume bounded journal space and source WAL.

Capture reports every 250 ms in continuous mode; source catalog/retention checks
remain once per second. Reports identify the published cursor used for backlog
measurement. A status reader does not combine backlog measured against an old
cursor with a newer publication. Worker restart clears prior progress until the
new process reports. Missing, future-dated or stale evidence cannot report healthy.

`sync show` and `sync list` add `continuous_status`:

| Field | Meaning |
| --- | --- |
| `state` | `initializing`, `catching_up`, `healthy`, `lagging`, `pausing`, `paused`, `unavailable`, `blocked`, `failed` or `deleted` |
| `observed_at_ms`, `stream_observed_at_ms` | Capture report and replication-stream receipt times |
| `source_barrier_at_ms` | Source commit time of the latest durably captured barrier |
| `published_lsn`, `captured_lsn`, `source_lsn` | Separate publication, complete-capture and observed source WAL boundaries |
| `oldest_unpublished_commit_age_ms` | Age of the oldest captured but unpublished **data** commit; zero only with fresh proof of no pending data; otherwise unknown is null |
| `lag_observed_at_ms` | Time the lag value was evaluated; null when evidence is unavailable |
| `backlog_bytes` | Unpublished journal payload, including barrier-only transactions; not a row count or a WAL-position difference |
| `spool_bytes`, `retained_wal_bytes`, `pressure` | Observed resource use; pressure starts at 80% of either capture quota |
| `head_changed` | Another workflow moved the branch head away from this capture's publication |
| `capture_keeps_compute_awake` | Capture continues to hold the compute lease, including while application is paused |

Freshness is an observed property, not a synchronous read-after-write promise.
An observed healthy cut can predate a just-committed transaction. The source proof
expires after the smaller of five seconds and the configured freshness budget;
stream/report evidence expires after five seconds. Snapshot age alone is never
replication lag. The reported age excludes changes not yet decoded; it is not a
complete end-to-end latency estimator. Unknown evidence remains visible. The
qualification harness separately measures source commit acknowledgment to atomic
publication using actual transaction IDs and complete commit end LSNs.

## Pause, lifecycle and recovery

Pause records `pause_requested=true` and a new policy revision. If a batch is
already admitted, that batch is explicitly authorized under the pause revision
to finish its unchanged target. No later batch is admitted. The policy becomes
`paused` after that complete boundary; an interrupted partial catch-up run is
cancelled at its last published prefix. With no admitted batch, pause completes
immediately. Poll the policy: the retry receipt itself is immutable and can still
show the earlier pause request. Updates/resume wait until the pending pause finishes.

Capture remains active while application is paused and still holds compute awake.
Resume reuses the checkpoint when source identity and retained history remain
valid. Cancelling an individual active run instead fences private work as in SY04
and pauses continuous admission; explicit resync may be necessary. Deletion fences
work and cleans up owned capture resources. Already published epochs stay readable
under their existing references.

Triggered/continuous mode changes reuse the same version-2 capture generation
only after active runs drain. Pause first, change the full configuration, then
resume. Crossing between snapshot and incremental modes still requires capture
retirement and a new bootstrap. Authority, source lineage and capture replacement
checks remain unchanged. A source sleep/outage cannot remain healthy; the daemon
can reconnect after wake/restart, but a retention gap requires explicit resync.

Catalog schema 27 fences older runtimes and adds a policy/run lookup index. Stopped
backup restore pauses policies, clears pending pause intent, preserves committed
publication and fences copied capture state. It does not automatically resume a
consumer against copied or unrelated source history.

## Qualification and remaining limits

The Rust tests cover automatic admission, one fixed target, idle epoch suppression,
graceful pause and mode conversion, stale evidence, lag, pressure and configuration
bounds. Python tests recover derived data progress from the checksummed journal
and distinguish barrier-only traffic from data backlog. Existing SY03/SY04
publication and spool crash tests remain applicable.

`e2e/native/continuous.py` runs a disposable native cell with two 10,000-row tables,
750 two-row transactions paced at 50 changed rows/s for 30 seconds, then a 200-row
burst. It records p50/p95/p99 commit-to-publication lag, OLTP transaction latency
before enrollment and under continuous load, achieved rate, sampled owned-process
RSS/CPU, allocated data bytes, spool and retained WAL maxima. Source XIDs are mapped
to durable complete end LSNs and the first published group covering each end.
The gate retains SY00's p95 <= 5 seconds and requires the burst within five seconds.

The [local Linux source report](sy05-evidence/linux-source.json) records eight
passing checks on 2026-09-22, with binary/engine and selected source-file hashes.
This x86_64 host exposed 16 CPUs; the two source tables occupied 1,277,952 bytes.
The final run sustained 50.05 changed rows/s:

| Measurement | Observed |
| --- | --- |
| Commit-to-publication p50 / p95 / p99 | 3.065 / 4.057 / 4.307 s |
| 200-row burst publication | 2.943 s |
| OLTP transaction p95 before enrollment / during continuous load | 8.225 / 12.838 ms |
| Sampled peak owned-process RSS | 1,503,891,456 bytes (whole native stack, not only the materializer) |
| Sampled peak allocated data | 372,629,504 bytes |
| Observed spool / retained WAL maxima | 245,760 / 358,440 bytes |

The OLTP phases report actual timings, not a causal overhead estimate: the baseline
uses a hot key before enrollment while the paced workload touches many keys.
GitHub [Linux/macOS native qualification](https://github.com/supabricks/platform/actions/runs/35797864550) and all required checks passed for SY05 commit `fdf57d3`; squash merge `124ce6a` contains that implementation.

Lifecycle cases include long transactions, pinned Sail readers, pause/resume,
a held materializer reporting lag, a stale/stopped capture worker, SIGKILL
replacement, a delayed controller tick, full daemon/compute restart, schema fencing,
explicit resync and cleanup. Linux/macOS CI runs this harness. Measurements are
source-level and hardware-specific; sampled RSS can double-count shared memory,
short-lived CPU peaks can be missed, and the disk sample excludes runtime archives.
They do not establish an indefinite-duration or installed-release SLA.

Finite SY03 budgets remain: 1,024 incremental runs and 4,096 retry receipts per
installation, 1 GiB roots, 4,096 files and fewer than 1,024 versions/table. Resync
does not erase installation histories. At these limits admission stops visibly.
Spool pruning, journal maintenance and Delta compaction/vacuum remain SY07;
continuous application is therefore a bounded engineering capability at this slice.
No background history deletion, hidden full-copy fallback or cross-group fairness
claim is added. Only one capture group is admitted.

The liveness distinction follows PostgreSQL's [replication protocol](https://www.postgresql.org/docs/17/protocol-replication.html).
