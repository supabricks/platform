# SP11 — sustained correctness, recovery and scaling

Status: implementation and qualification started, October 5, 2026. SQLite is the
selected implementation from the [SP10c review](sync-performance-evidence/2026-10-05-sp10c-review/README.md).
This does not waive SP10c's missing experimental sustained arms. SP11 measures
the selected SQLite path; it does not adopt RocksDB or expand the supported SLO.

## Execution sequence and scope

1. **Steady baseline (implemented; supervised campaign launched):** three fresh
   15-minute fixtures at each of 8/16 allowed logical CPUs, followed by one fresh
   60-minute fixture at each. Alternate CPU profiles between repeats. Offer 1,250
   changed rows/s with eight clients, 60 seconds warmup, 16 GiB cgroup memory and
   no swap. Use the exact previously qualified #157 SQLite-owner package so a
   runtime/package change cannot be mistaken for sustained behavior. Its identity
   remains separate from the current harness and merged source. This is local
   Linux performance evidence, not an exact newly assembled release qualification.
2. **Maintenance and readers (pending):** at least three observed successful
   checkpoint/reclamation cycles and an actual generation rotation. Exercise
   pin/unpin, Sail epochs held over rotation/GC, retained roots and post-unpin
   cleanup. A prune call count is not proof of a completed maintenance cycle.
3. **Capacity and catch-up (pending):** three repeats at 4/8/16 CPUs around
   50/250/500/1,000/1,500 rows/s, source-only ceilings, bursts, and at least
   30 seconds of apply pause with capture/input continuing. Preserve overloads.
4. **Workload and interference (pending):** wider/larger data, hot keys, inserts,
   deletes, primary-key changes, large supported transactions, many tables, idle
   sources, triggered barriers and concurrent OLTP/SQL/notebook/catalog readers.
5. **Fault qualification (pending):** group commit/feedback, journal read,
   table/descriptor/publication, pruning/checkpoint and relevant migration
   boundaries; pressure/ENOSPC, corrupt history, lost WAL, source restart and
   revocation. Verify prior coherent epochs and no missing acknowledged data.

A passing first campaign is only a steady-baseline result. It cannot complete
SP11, replace those other gates, establish EC2 scaling or qualify TPC-DS. Runtime
fixes found here require a separately measured slice before a fresh affected run.

## Predeclared steady analysis

`sp11_trial.py` reuses the existing workload, observer, shutdown, resource sampler
and retained-chain verifier. Profiling remains off. After the load/drain interval,
it exports a gzip stream of fixed 16-byte network-order records: two doubles for
COMMIT acknowledgement and first observed covering publication, in milliseconds.
No row payloads, SQL or credentials are recorded. The stream has a 128 MiB
uncompressed budget. This lets review independently recompute window results.
The existing durable-marker budget remains 256 MiB (about 64 MiB for a one-hour
1,250-row/s run plus warmup), without changing durability or product limits.

Evaluate 300-second windows every 60 seconds, plus a final aligned window. Each
window must commit **and publish** at least 1,000 changed rows/s. Publication
counts use actual wall-clock publication time, excluding a later drain; only
transactions originating in this measured source interval count. Lag uses each
window's COMMIT cohort, including its bounded subsequent publication. Require
p95 <=5,000 ms in every window and report p99/max and the worst window. This is
not a claim about every mathematically possible rolling interval.

Compare first/last five-minute observations. Before running, freeze these
investigation thresholds in `sp11_analysis.POLICY` and the campaign configuration:

- Late backlog p95 <=1.25 × early p95 +1 MiB; fitted backlog slope <=1 MiB divided
  by measured duration. Report full series and maxima, not just this screen.
- Late median cgroup memory <=1.25 × early median +256 MiB. Cgroup memory includes
  cache and is distinct from RSS. A failed growth screen stops for investigation.
- Sampled capture spool <=512 MiB; no resource inspection errors or sample gap
  exceeding five seconds (including interval edges). Sampling is not a hard quota
  and capture spool size is not whole-stack storage. Physical high-water,
  retained generations and pinned-reader behavior require the later phases.
- Existing complete table equality, source-rate accounting, whole-run freshness,
  120-second maximum drain, foreign-key checks, journal reopen and zero leaked or
  remaining owned descendants must pass. A mean cannot hide a failed repeat.

## Supervision and evidence

`sp11_campaign.py` validates frozen harness, runtime manifest/binary, qualifier
image, host topology and free disk. One host monitor carries quiet credit across
trials; only real competing activity or a sampling gap resets it. It emits
10-second heartbeats while waiting/running, archives hashes/cleanup/contention
for every attempt and stops on the first failed or contaminated fixture. It does
not restart or silently replace measurements. SIGTERM stops its owned container even if the Docker client has already exited
([#166](https://github.com/supabricks/platform/issues/166)); the service uses
`KillMode=mixed` so the controller can finish this cleanup. Launch 01 was stopped
during quiet admission before any fixture began. Its frozen inputs/status remain
retained; launch 02 uses the corrected supervisor.

Eight steady fixtures contain **210 minutes of measured load**, plus eight warmups,
startup/cleanup and one initial quiet admission (roughly four hours without
contention). A stopped campaign is evidence requiring investigation, not a pass.
A ten-second installed screen checks plumbing and teardown only; its gates never
qualify throughput or SP11. Live status is stored under
`build/sp11-20261005/steady-02/status.json`; the [frozen manifest and launch record](sync-performance-evidence/2026-10-05-sp11-start/README.md) are archived separately.
