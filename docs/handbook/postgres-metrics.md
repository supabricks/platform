# PostgreSQL metric history

PG247a supplies installation-local history for the console Observability tab and
other clients. It is a backend contract; the product console charts are separate.

```sh
supabricks metrics history --branch main --range 1h
supabricks metrics history --branch main --range 7d
```

API action `metrics`, console workspace command `metrics`, and MCP tool `metrics`
all accept the same command:

```json
{"action":"history","target":{"branch":"<branch UUID>","revision":1},"range":"6h"}
```

The explicit target must belong to the selected project and have the current
revision. This slice supports local operator authority. Governed branches are
excluded from collection and reads until governed monitoring is implemented.
Internal export branches, deleted branches, and expired branches are excluded.
Reads never connect to PostgreSQL or wake a compute.

## What is measured

| Field | Meaning |
| --- | --- |
| `connections_active` | Active client backends in `postgres`, excluding this sampler |
| `connections_idle` | Idle client backends, including idle-in-transaction states |
| `connections_other` | Remaining client backend states, including unknown state |
| `commits_per_second`, `rollbacks_per_second` | Database counter delta divided by actual elapsed seconds |
| `wal_bytes_per_second` | Compute-wide generated WAL delta; includes other databases |
| `cache_hit_ratio` | Interval PostgreSQL shared-buffer hits / (hits + reads); null with no accesses |
| `database_size_bytes` | `pg_database_size('postgres')`, not the installation/object-store footprint |

Transaction counters include monitoring and other platform traffic. No artificial
subtraction is applied. PostgreSQL publishes cumulative statistics asynchronously,
so these are sampled observations, not an exact event stream. Sources and scope
follow the [PostgreSQL 17 statistics documentation](https://www.postgresql.org/docs/17/monitoring-stats.html).

CPU, memory and query latency percentiles have explicit `unavailable` reasons.
Statement means from `activity statements` cannot supply p95. There is no SQL
text, query fingerprint, table name, password, or external telemetry in history.

## Chart contract

Ranges are `1h`, `6h`, `24h`, and `7d`. Bucket widths are respectively 30 seconds,
30 seconds, 1 minute, and 10 minutes, with at most 1,441 returned points. Aligned
ranges include the current partial bucket. `aggregation` is
`last_observation_in_bucket`: a coarse point is the last sampled observation in
that bucket, **not** a bucket mean or full-bucket transaction rate. `samples` and
`available_samples` expose partial coverage; clients must not interpolate missing
buckets. `observed_at_ms` is the observation time, distinct from bucket start.

Missing, suspended, not-ready and failed samples have null metric values, never
zero. Healthy quiet intervals can have zero transaction/WAL rates. Rates need
consecutive valid observations within 90 seconds and the same daemon generation,
branch revision, postmaster start and database OID. A database/WAL stats reset or
any counter regression invalidates the interval. The next valid pair restores
rates. `rates_status` and `rate_interval_ms` explain this boundary; gauges remain
available during rate warm-up. History survives daemon restart, but rates do not
bridge that restart. Forward/backward clock jumps cannot create negative rates.

## Resource and retention policy

- Target cadence: 30 seconds; two workers; no unbounded request queue. Slow
  collection can delay cadence, leaving visible gaps. Each connection/read has
  a four-second total deadline, 1.5-second statement and 250-ms lock timeouts.
- At most 64 eligible branches, ordered by branch UUID. Additional branches return
  `collection_admitted: false`; existing retained history remains readable.
- At most seven days and 131,072 samples **across the installation**, with oldest
  samples evicted under capacity pressure. One sample per branch per 30-second
  slot bounds restart storms. At 64 continuously sampled branches, the row cap
  retains about 17 hours, not seven days. `oldest_retained_at_ms` reports actual
  retained coverage. Longer high-branch-count history requires later rollups.
- Private `metrics.sqlite3` in the data root, capped at 256 MiB of database pages,
  with a 2-MiB SQLite page cache. The temporary rollback journal needs additional
  space (up to the database size); no telemetry WAL grows behind pinned readers.
  Pruning runs every minute, including when no branches remain. Freed pages are
  reused; the file need not shrink after eviction.

This is a separate disposable observation database, using the existing SQLite
library. It carries no control authority, CDC payload or query execution state
and introduces no control-store migration. The daemon is its sole writer.
Writes cannot block PostgreSQL workers; a storage failure marks
`storage_healthy: false` while preserved history stays readable. Failure to open
the store disables metrics, without preventing daemon startup. Stop the daemon
before replacing/removing a damaged metrics database; removing it loses only
observability history. Read failures are returned, never fabricated as empty data.

## Remaining #247 slices

- **247b:** compute CPU/memory ownership and process-tree accounting on Linux and
  macOS, with resource-limit semantics and lifecycle-safe identity checks.
- **247c:** actual latency distributions/percentiles, selecting instrumentation
  and validating its collection overhead; no estimates from statement means.
- Governed monitoring authorization and product console charts remain separate
  integration work. Large-installation rollups are needed for guaranteed seven-day
  coverage beyond the documented raw-sample capacity.
