# SP03b capture WAL qualification and attribution

Status: **complete — keep for performance**, 2026-09-26. SP03a merged in
[PR #109](https://github.com/supabricks/platform/pull/109); SP03b is delivered in
[PR #111](https://github.com/supabricks/platform/pull/111).

WAL/FULL with the existing grouping reduces capture native sync calls per
transaction by about 44% under overload and raises achieved source throughput by
8–9% in fresh pairs. All 24 main trials pass correctness and five-second p95
freshness. The tradeoff is roughly 7–9% more total CPU across the tested cells;
physical headroom also reduces usable row capacity. Keep the bounded WAL policy
and grouping together. The fixed four-client workload reaches about 742–746
changed rows/s, so sustained 1,000 rows/s remains unqualified. SP04 is next.

All 72 full-stack and 36 component/control measurements are complete, with no
detected build/test overlap. Deliberately ungrouped variants account for three
DELETE drain timeouts and three WAL freshness misses; every grouped full-stack
trial passes. Thirty earlier component measurements and two admission attempts
with no trials are retained separately. The protocols below were declared before
their measurements. Signed installed WAL/sync qualification passes on Linux and
macOS. The separate frozen-head catalog-probe failure remains tracked in #110.

Only the capture spool changes to WAL/FULL. PostgreSQL, Delta and the publication
catalog keep their existing roles. Production grouping stays at 32 complete
transactions, 1 MiB or 10 ms. The fixed SQLite dependencies qualified by SP03a
remain unchanged.

## Completed fresh predecessor comparison

All 24 mandatory trials passed correctness and the five-second p95 target, with
no detected host contention or replaced pairs. Each table entry is the median of
three trials; percentage changes below use matched pairs, not ratios of medians.

| CPUs | Offered rows/s | Actual rows/s, SP03a → SP03b | p95 lag, SP03a → SP03b | Paired CPU change | Paired peak-memory change |
| --- | ---: | ---: | ---: | ---: | ---: |
| 4 | 50 | 50.036 → 50.038 | 3.713 → 3.499 s | +7.82% | −1.81% |
| 16 | 50 | 50.038 → 50.038 | 3.635 → 3.377 s | +9.11% | +0.18% |
| 8 | 1,000 | 686.114 → 741.856 | 4.539 → 4.524 s | +7.03% | −4.80% |
| 16 | 1,000 | 684.649 → 745.789 | 4.560 → 4.465 s | +6.71% | +5.31% |

Paired median achieved-source-rate gains under overload are +8.12% and +9.08%.
All twelve overload trials still miss offered input; none qualifies sustained
1,000 rows/s. Paired p95 changes are −5.77%, −7.10%, −0.05% and −2.02% in the
same table order. Three repeats are screening evidence, especially for the small
high-load latency differences. Full individual values, ranges and matched changes
are retained in the [main comparison](sync-performance-evidence/2026-09-26-sp03b/main/comparison.json).

Checkpoint-inclusive capture analysis reduces COMMIT-plus-checkpoint wall time
per transaction by 46.71% / 46.37% at 8/16 CPUs under overload. Total native sync
calls per transaction, including work outside those two stages, fall by 43.87% /
43.83%, and total native sync time falls by 43.75% / 43.89%. At low load, total
native sync calls fall by about 66%. Explicit checkpoint work is counted, not
hidden in an apparent COMMIT-only saving. The generic comparison retains its
legacy COMMIT-only metric names and checkpoint capability label; use the
[additional WAL analysis](sync-performance-evidence/2026-09-26-sp03b/main-wal-paired-summary.json)
for inclusive costs and [raw invariant checks](sync-performance-evidence/2026-09-26-sp03b/main-wal-summary.json)
for physical size and feedback. The largest sampled main-candidate physical
footprint was 6,139,888 bytes; no backpressure or incomplete-checkpoint counter
was observed. Candidate samples satisfy FULL, total sidecar accounting and the
configured physical limit. Comparable sampled feedback never exceeds durable.

The CPU increase is a measured tradeoff even though no cell crosses the 10%
investigation threshold. At overload, groups contain about 7.5–7.6 transactions
with WAL versus 15.7–15.9 with DELETE under the unchanged 10-ms policy: faster
commits produce more group operations as well as more captured transactions.
Capture CPU rises from median 4.78 → 6.18 seconds at 8 CPUs and 4.60 → 6.04 at
16 CPUs within matched load windows. At low load capture CPU decreases slightly,
while successful apply workers started during load increase from median 28 → 31
at 4 CPUs and 29 → 30 at 16 CPUs. Their lifecycle CPU rises from 26.62 → 31.01
and 48.97 → 50.94 seconds. This supports increased downstream work as part of
the resource tradeoff, not an exclusive causal attribution: worker lifetimes can
cross the load window, and sampled process counters miss short-lived tails.
[Worker/stage analysis](sync-performance-evidence/2026-09-26-sp03b/main-profile-analysis.json)
and [sampled process CPU](sync-performance-evidence/2026-09-26-sp03b/main-resource-analysis.json)
retain these accounting limits. Do not equate their totals with cgroup CPU.

The next constraint remains downstream work and source durability. At overload,
median per-worker directory timings remain about 718–724 ms within 980–988 ms
apply runs, and complete-trial p95 commit-to-admission times remain about 2.48 s.
Stage percentiles must not be added together. Source SQL COMMIT p95 falls from
18.19 → 17.28 ms at 8 CPUs and 18.43 → 17.26 ms at 16 CPUs, consistent with reduced
shared durability pressure, but not proof of a unique source bottleneck. SP04
still targets repeated directory scans; SP06 separately qualifies source capacity.
Candidate overload medians barely change from 741.856 rows/s at 8 CPUs to 745.789
at 16 CPUs with the fixed four-client profile; these cells do not establish useful
throughput scaling from the additional cores, and do not emulate EC2 instance types.
The diagnostic changed-file size delta is +10,715 bytes including checked-hash
bytecode; the native binary itself is 18,144 bytes smaller. These are logical
overlay sizes, not compressed release size or physical hardlink disk usage.
[Size accounting](sync-performance-evidence/2026-09-26-sp03b/packages/size-delta.json)
retains each changed file. The interaction and observer controls below support keeping this slice with
its measured CPU and physical-capacity tradeoffs.

## Completed component factorial

All 30 final-candidate component trials passed correctness and the physical bound,
without detected contention. Median saturated transaction throughput across three
repeats was:

| Journal | One transaction per commit | Grouping retained from SP02 |
| --- | ---: | ---: |
| DELETE/FULL | 52.23 tx/s | 1,315.99 tx/s |
| WAL/FULL | 154.47 tx/s | 3,759.05 tx/s |

At 500 offered transactions/s, grouped DELETE and WAL achieved medians of 499.07
and 499.68 tx/s. Neither ungrouped variant sustained that input. The grouped WAL
saturated result is approximately 2.86 times the grouped DELETE result. Native
syncs per transaction, including pruning and checkpoints, fell from 0.15534 to
0.03970 in that saturated comparison. This is component attribution on the same
SP03b implementation; the measured full-stack gain above is substantially smaller.

Separate grouped-WAL saturated activation controls measured median throughput of
3,786.89 tx/s with native profiling disabled and 3,781.71 enabled. Three pairs are
an overhead screen, not an equivalence claim. Disabled native sync counters are
unavailable, not evidence of zero sync calls. Every run independently verified
retained payloads, ordered links and pruned-prefix accounting after the writer
stopped. [Individual receipts and reproducible summary](sync-performance-evidence/2026-09-26-sp03b/component-screen/README.md).

## Grouping and journal-mode interaction

All six trials ran without detected contention. Ungrouped DELETE timed out in
publication drain in all three repeats; grouped DELETE passed correctness and
freshness in all three. The failures retain their complete source/load profiles,
backlog observations and cleanup evidence, and have no complete latency percentile.

Median capture rate was 29.22 transactions/s without grouping versus 344.29 with
it. Actual source rates were 649.83 versus 688.12 changed rows/s; neither reached
1,000. Grouped p95 lag was 4.538 s. This is a failure-to-completion result on the
same SP03b implementation, not a percentage latency speedup. It confirms the
continued need for SP02 grouping in DELETE mode. [All six receipts](sync-performance-evidence/2026-09-26-sp03b/delete-ablation/comparison.json)
and [profiles](sync-performance-evidence/2026-09-26-sp03b/delete-ablation-profile-analysis.json)
are retained.

All six WAL ablation trials also ran without detected contention. Ungrouped WAL
completed correctly in all three, but its p95 lag ranged from 92.304 to 93.766 s,
missing the five-second target every time. Grouped WAL passed correctness and
freshness in all three. WAL alone improves the diagnostic from DELETE timeout
to eventual completion; it does not replace grouping.

| Diagnostic configuration, 16 CPUs / 1,000 offered rows/s | Complete | Fresh | Median actual rows/s | Median capture tx/s | Median complete-trial p95 lag |
| --- | ---: | ---: | ---: | ---: | ---: |
| DELETE, ungrouped | 0/3 | 0/3 | 649.834 | 29.22 | unavailable: drain timeout |
| DELETE, grouped | 3/3 | 3/3 | 688.122 | 344.29 | 4.538 s |
| WAL, ungrouped | 3/3 | 0/3 | 667.936 | 91.67 | 93.672 s |
| WAL, grouped | 3/3 | 3/3 | 751.972 | 376.61 | 4.542 s |

[WAL receipts](sync-performance-evidence/2026-09-26-sp03b/wal-ablation/comparison.json)
and [profiles](sync-performance-evidence/2026-09-26-sp03b/wal-ablation-profile-analysis.json)
retain every result. These matched diagnostic pairs at one overload cell establish
the interaction; their improvements must not be added as independent percentages.
All six intentionally ungrouped variants fail freshness, including the three
DELETE timeouts. Every grouped full-stack trial in the entire series passes
correctness and freshness. No diagnostic variant reaches 1,000 source rows/s.

## Completed predecessor profiler controls

All 18 SP03a off/on controls passed correctness and the five-second p95 freshness
gate without detected contention. The common checkpoint-aware profiler's paired
median CPU overhead was +4.13% at 4 CPUs / 50 rows/s, +3.34% at 8 CPUs / 1,000
rows/s and +1.83% at 16 CPUs / 1,000 rows/s. Paired peak memory changes were +1.89%,
+2.73% and +1.89%; p95 latency changes were −0.67%, +1.05% and +1.23%. None crosses
the predeclared 10% investigation threshold. These are activation screens on the
unchanged predecessor; they do not measure WAL's contribution.

[Individual predecessor controls](sync-performance-evidence/2026-09-26-sp03b/predecessor-controls/comparison.json)
include complete raw receipts, profiles and host observations. These controls
measure activation cost, separately from the runtime comparison above.

## Completed candidate profiler controls

All 18 WAL candidate off/on controls passed correctness and freshness without
detected contention. Paired median CPU overhead was +2.90% at 4 CPUs / 50 rows/s,
+4.03% at 8 CPUs / 1,000 rows/s and +4.22% at 16 CPUs / 1,000 rows/s. Paired peak
memory changes were +1.84%, +7.69% and +2.99%; p95 latency changes were +3.47%,
+0.06% and +1.90%. No median crosses the 10% investigation threshold.

Every retained candidate status sample stayed within its physical budget.
The largest recorded spool footprint across these trials was 6,221,928 bytes
against 536,870,912 configured bytes; no backpressure or incomplete-checkpoint
counter was observed. Every comparable feedback/durable profile sample preserved
feedback <= durable. Checkpoint timing and native sync calls are retained
separately from COMMIT and included by the additional WAL analysis.

[Candidate controls](sync-performance-evidence/2026-09-26-sp03b/candidate-controls/comparison.json)
and [physical/checkpoint/feedback validation](sync-performance-evidence/2026-09-26-sp03b/candidate-controls-wal-summary.json)
are archived. These establish profiler activation cost and invariants on the
candidate; the fresh predecessor/candidate matrix provides slice attribution.


## Supplemental read-observer controls

All six separately predeclared component controls passed independent stopped
correctness and physical bounds without contention. Both arms disable native
profiling. Median throughput was 3,799.21 tx/s with the read observer off and
3,785.19 tx/s with it on; the paired median change is −0.43% (range −0.60% to
+0.02%). Paired CPU cost is +2.94% (range +2.16% to +3.82%). This is a three-pair
overhead screen, not proof of equivalence or an end-to-end capacity measurement.
Disabled native sync counters remain unavailable.

One reader-on repeat recorded one incomplete checkpoint, then a complete PASSIVE
checkpoint. It retained 14,324,144 physical bytes, including 14,131,632 WAL bytes,
versus 7,270,944 bytes in its paired reader-off run. The other reader-on runs ended
near 7.25–7.27 MB. Every run stayed within the 536,870,912-byte budget with no
backpressure. A completed PASSIVE checkpoint can leave a larger WAL allocation
for reuse; completion does not imply truncation. This observed storage cost is
tracked for a sustained SP05 experiment in [#116](https://github.com/supabricks/platform/issues/116),
without changing the frozen policy from this short screen.

[Individual receipts and summary](sync-performance-evidence/2026-09-26-sp03b/reader-controls/summary.json)
and the retained driver/host observations reproduce the result. The source plan
is [the predeclared supplement](sp03b-observer-control-protocol.md).

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
checkpoint growth and at least 1 MiB for shared memory (or its larger extant size),
in addition to existing sidecars. An inherited total above the configured physical
limit is refused before opening SQLite.
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
chain, then requires an exclusive reader lease and write-headroom admission before
changing mode. Low free space preserves the original mode and committed prefix. An existing
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

A separately [predeclared supplement](sp03b-observer-control-protocol.md) adds six
component read-observer controls after the original series. It changes neither
these 30 component trials nor the 72 full-stack trials.

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

## Qualification and retained variants

The final measurement candidate and common harness are frozen at `5382e80`.
Local validation passes 85 analytics tests, including 12 WAL fault tests; the
installed diagnostic worker also passes those 12 tests. Rust validation passes
206 tests with four ignored; its source is unchanged by the later Python-only
admission fixes. The performance harness passes 39 tests. Signed Linux/macOS
release qualification and the full-stack performance series passed their
production-candidate gates; all six supplemental observer controls passed too.

The first candidate (`1debe4f`) completed all 30 component trials correctly and
without detected contention. Those results are retained under
[superseded evidence](sync-performance-evidence/2026-09-26-sp03b/superseded/component-1debe/README.md)
and excluded from final acceptance. Review then added explicit migration
headroom admission and accounting for inherited sidecar sizes before opening or
appending ([#113](https://github.com/supabricks/platform/issues/113)). No full-stack
trials ran on earlier candidates; the final candidate repeats the whole declared
series with unchanged batching and checkpoint settings. Two earlier harness
admission attempts ended before their first measurement and are retained too.

The stopped-spool corruption fixture now writes through the configured analytical
Python, while the host interpreter remains a read-only inspector
([#112](https://github.com/supabricks/platform/issues/112)). The initial CI failure
was an assertion expecting an OS error after unsafe-path rejection had moved ahead
of the OS open call; the corrected assertion and both platform analytics jobs pass.

## Installed platform qualification

Both signed-install sync gates passed. The Linux x86_64 and macOS arm64 packages
each passed all 12 capture WAL fault checks and all four triggered, continuous,
maintenance and governed sync suites. The release evidence validator accepted
both complete receipts, including cleanup, offline isolation, workload/freshness,
worker inventory and release identity. Python loads SQLite 3.53.1; Rust loads
3.53.2, with unchanged reviewed dependency identities.

CI built merge commit 00af9d3, whose Git tree is identical to frozen runtime
5382e80. [Source-tree proof](sync-performance-evidence/2026-09-26-sp03b/ci/source-identity.json),
[Linux validated evidence](sync-performance-evidence/2026-09-26-sp03b/ci/linux-validated-sync-evidence.json)
and [macOS validated evidence](sync-performance-evidence/2026-09-26-sp03b/ci/macos-validated-sync-evidence.json)
are retained with the original compressed receipts and artifact identities.

The broader Linux catalog probe failed at the governed post-restore request
tracked in #110, this time with invalid_input; common cause with the earlier
conflict remains unestablished. Its same-head retry repeated the same invalid_input error. The Linux
notebook-environment cancellation fixture separately failed while checking a
process ownership token (#114); one same-head retry passed all manager/kernel checks.
The macOS project-portability consumer failed
its first deployment without an exported operation-step diagnostic (#115); its
same-head retry passed all fifteen consumer checks. The native-release workflow
therefore passed on attempt two. The separate Linux catalog probe on this frozen runtime head remains failed.
All first failures and all three bounded same-head retries are retained. These are
separate from the passed installed capture/sync gates; they are not silently
discarded or treated as evidence of a WAL regression.
