# Database compute and storage

[Handbook](README.md) · [Database workspace](database-workspace.md) ·
[API contract](../../api/domains/databases/paths.yaml)

Issues #244, #245 and #246 add native local-owner backend controls for the
console's provisioning, Compute and Branches screens. The clickthrough console
is a separate repository; these contracts do not claim its UI is connected.

## Compute sizes

`GET /api/v1/compute-sizes` and local `capabilities.compute` report the catalog,
its default, per-size host availability and the shared-host resource model.

| Size | CPU tuning budget | Memory tuning budget | Gateway connections |
| --- | --- | --- | --- |
| `s` | 1 CPU | 1 GiB | 32 |
| `m` (default) | 2 CPUs | 2 GiB | 64 |
| `l` | 4 CPUs | 4 GiB | 128 |

These are PostgreSQL tuning profiles, not CPU reservations or process memory
limits. PostgreSQL uses a quarter of the memory budget for shared buffers, three
quarters as an effective-cache estimate, 4 MiB work memory per executor operation,
and one sixteenth for maintenance work. Parallel workers follow the CPU budget.
Concurrent sorts, backend overhead and caches can exceed the nominal RAM budget;
CPU remains shared with the rest of the installation. PostgreSQL accepts 16
additional internal connections beyond the gateway limit, eight of them reserved
for superusers. The gateway enforces each branch's client limit and the existing
256-client installation limit. Unused per-branch slots do not reserve global slots.

Explicit creation and resize refuse sizes above detected host CPU or memory
capacity. On Linux the memory check also considers the process's unified cgroup
limit when available. This check is not admission against the sum of all branch
budgets or a promise of currently free memory. The default requires a host with
at least two available CPUs and 2 GiB; choose `s` on a smaller supported host.
Governed callers cannot provision, resize or configure idle suspension through
these endpoints; they receive `unavailable_in_profile`. Existing governed
branches retain the default local engine tuning. A future isolated compute
runtime must declare a different resource model.

Database and branch creation accept `compute_size` and `idle_timeout_seconds`.
Omitting them chooses `m` and never suspend, independently of a branch's parent.
The settings, branch identity, credentials, ports and operation commit together.
Older CLI/project creation continues to choose the default.

Read a database or branch to obtain its ETag, then PATCH the same URL:

```json
{"compute_size":"l","allow_restart":true}
```

Send the explicit ETag in `If-Match` and a fresh `Idempotency-Key`. `If-Match: *`
is refused for compute changes. Send compute settings separately from name or
expiry edits. The response is `202` with the durable operation, including for
idle-only changes. Repeating the same request/key returns the original receipt;
a changed request is refused.

A running resize requires explicit restart acknowledgment. Drain gateway clients,
work leases, imports, branch pins, pending project apply and capture first. The
operation captures the durable WAL boundary, stops and retires the old compute,
and starts the same endpoint and timeline with the new profile. The gateway
address and credentials remain unchanged. Connections arriving after acceptance
wait for the replacement. Failed steps remain retryable in the existing operation
journal. A suspended resize stays suspended and applies when compute next starts.
`compute.size` is desired configuration; wait for operation success before treating
it as applied. Idle-only changes increment the API revision without restarting.

## Idle suspension

`idle_timeout_seconds` accepts an integer from 60 to 86400, or `null` for never.
Existing branches migrate to never; no previously running branch is opted in.
The daemon uses a monotonic timer and resets it after an owner restart, a lifecycle
change, a setting change, a connection or protected work. Activity counters catch
connections/jobs that start and finish between scheduler ticks.

The entire interval must pass with no gateway connections, work leases, imports,
active branch pins or pending project apply. A capture generation holds its source
awake until it is deleted, including while paused: paused capture still retains
replication state and may need cleanup. `compute.capture_holds_awake` explains
that refusal. Delete the capture generation before resizing or allowing its
source to auto-suspend. These controls cover supported gateway clients and leased
platform work; bypassing the gateway through private backend ports is unsupported.

An expiry submits a durable `database.auto_suspend` or `branch.auto_suspend`
operation. The journal rechecks eligibility. `compute.last_suspend_reason` is
`idle_timeout` or `manual`; `last_suspended_at_ms` records request time, not confirmed
retirement. The normal branch state/operation shows progress. Gateway connections
wake suspended compute through the existing deduplicated wake sequence. Reading
configuration or storage does not wake it.

## Storage and connections

Every database/branch view includes `storage` and `connections`. Connections are
current gateway leases, including pending handshakes and wake-up waits, with the
selected profile's limit. They are not `pg_stat_activity` backend totals.

A bounded background worker samples pageserver timeline details about every 30
seconds, without SQL queries. Each request has a two-second deadline and bounded
response body. Samples become `stale` after 120 seconds or an owner restart. An
unsampled, failed or incomplete observation is `unavailable`; unknown bytes are
`null`, never zero. Sampling timestamps use Unix milliseconds. Logical values may
be estimates until `logical_size_accurate` is true.

- `logical_bytes`: the branch's logical size, including data inherited from parents.
- `exclusive_physical_bytes`: this timeline's layer files, excluding ancestor layers.
  Local and object-storage copies of the same layer are counted once. This is not
  a bytewise difference in changed rows.
- `storage.database.total_physical_bytes` on a database: sum of distinct live
  timelines in its branch tree. Logical clone sizes are never summed. If any
  component is unavailable the total is null; any stale component makes the total
  stale. Its timestamp is the oldest component sample. A deleting database can
  have zero live components and an unavailable total.

This scope excludes WAL, compute caches, temporary exports, backup-only timelines,
analytical tables and object-store overhead. It is database timeline accounting,
not total installation disk usage or billable storage.

## Recovery and integration

Schema 34 adds compute settings, activity counters and storage samples through the
existing verified stopped-backup upgrade path. Older binaries cannot reopen the
new catalog. Storage samples are owner-generation tagged; idle deadlines are not
restored from wall-clock timestamps. Native restart and resize reuse existing
WAL durability and process ownership fencing.
