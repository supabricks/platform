# Local state and operations

P02 added a single-writer state daemon to `supabricks-local`, with durable local
identity, intent and a resumable worker protocol. P03 connects that journal to the
[native storage cell](../architecture/native-cell.md); P04 adds
[database and exact-position branch operations](../architecture/native-branches.md). An unconfigured daemon
still reports `engine_execution: false` and only queues intent. Use the
[native setup instructions](../../e2e/native/README.md) to enable engine execution.
Public database/branch commands and explicit-LSN child branching follow in P04/P05.

From the repository root:

```sh
cargo run --locked -p supabricks-local -- daemon --data-dir /tmp/supabricks-p02-demo
```

The daemon creates a private data directory on the local filesystem. An existing
directory must belong to the current user and have mode `0700`. It holds an OS
lock on `owner.lock` for its entire lifetime, before opening SQLite, migrating or
replacing a stale socket. A second daemon exits with an ownership conflict.
Never unlink the lock file to resolve a conflict: doing so could create two
owners of different inodes. A killed daemon releases its lock automatically.

`state.sqlite3` uses bundled SQLite, WAL journaling, `synchronous=FULL` and foreign
keys. The DB, WAL, shared-memory file and socket are private. Symlinked database
and lock files are rejected. Each successful store opening advances a persisted
owner generation; old process evidence and leases survive rather than being
silently reassigned to the new owner.

## Public project identity and private checkout state

`ProjectConfig::initialize` atomically creates `supabricks.toml` without replacing
an existing file. Retrying reads the same ID. Only these public fields are allowed:

```toml
format_version = 1
id = "d01a12ac-ab7b-40a9-98e8-1ef30e99bf23"
name = "orders"
```

Register that configuration with the daemon. Branch names are scoped to the
project UUID; renames preserve branch, tenant, timeline and endpoint IDs.
`select_worktree` records the canonical checkout path and selected branch in
private SQLite state. Two Git worktrees may share the project file and select
different branches. Selection and connection lookups validate the project ID;
connection resolution always takes an explicit branch ID. A pending or suspended
endpoint does not produce a ready connection.

Per-endpoint credentials are generated once in the same transaction as endpoint
allocation. They live independently of compute files and are never included in
public project files, operation requests or the socket's status responses.
The engine adapter can retrieve them through the store library.

## Durable worker protocol

`submit(project_id, idempotency_key, mutation)` commits intent, resource identities,
credentials, port reservations and lifecycle revision atomically. Reusing a key
with the same normalized request returns the original operation, including after
completion. Different parameters conflict. Duplicate names or ports roll back the
whole transaction, leaving the key available for a corrected request.

Workers ask for a ticket, perform one idempotent effect outside SQLite, then submit
a checkpoint. The ticket identifies operation, step, branch, lifecycle revision
and owner generation. Its stable effect key is `operation_id:step_index`.
Delivery is **at least once**: an effect may have completed before its checkpoint
was committed. Engine adapters must inspect/reconcile the same resource when a
step is retried. P02 does not provide exactly-once external execution.

A new suspend/delete intent increments the branch revision and supersedes pending
older operations. Checkpoints from old revisions or owner generations are rejected.
A replayed checkpoint is accepted only with the same result. Metadata fencing
cannot stop an external process that is already running: the P03 supervisor adapter
verifies surviving OS process ownership before allowing a replacement writer.

The journal currently describes ensure-timeline/start-compute and
stop-compute/delete-timeline effects. The native adapter executes these effects
for root timelines. Parent branch references are persisted; P04 must resolve and
record a safe branch boundary before executing a child timeline creation.
Lifecycle revisions fence runtime changes; a display-name rename does not
invalidate work in progress.

Deletion retains ordered cleanup across restart. Ports and credentials are released
only after the final deletion checkpoint, and the name then becomes reusable.
Branch and operation tombstones remain so old idempotency keys cannot recreate
resources. Parent deletion is rejected while child cleanup remains incomplete.
Active work leases block suspend/delete; expired leases cannot be renewed.

## Schema and retained work

Embedded migrations run together in one transaction using SQLite `user_version`.
Version 1 contains projects, branches, endpoints/ports, credentials, checkout
selections and the operation journal. Version 2 adds process evidence, epochs,
table mappings and work leases. Version 3 adds native process evidence for shared
storage services and their supervisor, alongside endpoint process records.
A newer schema is rejected without resetting data;
a failed migration rolls back all its changes, including the version marker.

Process records include endpoint, role, owner generation, resource revision, PID,
process group and OS start identity. Recording a replacement cannot overwrite old
evidence. Removing a record requires matching its complete identity; the native
adapter also verifies that its entire OS process group has stopped. A shutdown checkpoint
is refused while its endpoint still has process records.

Epoch IDs, source LSNs and table mappings are immutable metadata. They do not mean
that an analytical snapshot has been published. Leases identify holder, branch,
optional epoch, generation and expiry; cross-branch epoch references are rejected.
Old-generation leases remain protective until expiry or explicit release, but
cannot be renewed by a restarted daemon. Publication, retention, lease adoption
and metadata garbage collection are later work.

## Private socket protocol

`control.sock` accepts one newline-terminated JSON request per connection, up to
64 KiB, with a two-second read/write timeout. This is a same-user engineering API,
not the public CLI/MCP contract. It exposes registration, intent submission,
operation/branch inspection, rename, checkout selection and shutdown. Native
children also use a private launch-authorization request; their identity commits
before they receive permission to execute. Workers
cannot acknowledge effects through the socket; checkpointing is an internal API.

For example, while the daemon runs:

```python
import json
import socket

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect('/tmp/supabricks-p02-demo/control.sock')
    client.sendall(b'{"version":1,"request":{"method":"status"}}\n')
    print(json.loads(client.makefile().readline()))
```

The response has `version` and either `result` or a structured `error`. To stop
gracefully, use the same envelope with `method: "shutdown"`. Pending intent remains
in SQLite. After shutdown, preserve the entire private data root for a backup;
do not copy only the SQLite main file while the daemon is writing to its WAL.

## Verification

`just portable-check` runs on Linux and macOS in CI without Kubernetes or a UI
build. The recovery tests use an idempotent fake engine and real subprocess kills
before effects, after effects and after checkpoints for create and delete. They
verify resumption and stable identities across all 12 boundaries. Separate tests
race daemon startup, send eight simultaneous duplicate requests, kill/restart the
daemon, and check migration rollback, stale workers, leases, process evidence,
worktree isolation, credentials, port reservations and cleanup.

These tests qualify the state/journal protocol. The separate
[native suite](../../e2e/native/README.md) covers real Neon processes, S3 operations,
SQL authentication, crash recovery and cold restore; Linux CI also injects actual
ENOSPC on a bounded filesystem. These are not power-loss guarantees.
