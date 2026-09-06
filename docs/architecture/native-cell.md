# P03 native storage cell

`supabricks up` starts or reconnects to the single-writer daemon and waits for
storage readiness. `status` reports readiness, operation backlog and owned process
IDs without returning credentials. `down` stops the entire cell; when the daemon
has died, it acquires the data-root lock and reconciles surviving processes directly.
The engineering control socket still accepts P02 project and operation requests.
[P04](native-branches.md) adds native database and head/LSN/time branch operations.
The stable connection gateway follows in P05 and public CLI/MCP commands in P06.

## Process ownership

The daemon owns desired state and the SQLite operation journal. Process Compose
1.122.0 supplies process execution, readiness probes, logs and dynamic project
updates through its loopback API, authenticated with a private random token.
Auto-restart is disabled: an intentionally suspended compute stays suspended.
The daemon decides when a stopped process should run again.

Process Compose executes the same `supabricks` binary's internal `child` entrypoint
using argv, without a shell. Before executing an engine binary, the child requests
launch authorization on the private control socket. The daemon verifies its
launch token, UID, process group, OS birth identity, root, owner generation and
branch revision. It commits the process evidence and endpoint process row in one
SQLite transaction before replying. Process Compose itself uses an equivalent
stdin gate so it cannot start children before its own identity is durable.
A closed gate has no engine effect. Unanswered/stale requests cannot start writers.

On recovery, stop the verified old supervisor first so it cannot race the new
owner. Stop each verified service leader, then verify and drain the entire process
group, including orphaned Postgres descendants, before removing its record.
Linux identities include boot ID and process start ticks; macOS uses native
`proc_pidinfo` birth seconds/microseconds. PID alone never authorizes a signal.
Unexpected identities or unmarked survivors prevent replacement writers.
Neon's WAL redo sandbox clears its environment: stop its verified pageserver
parent and wait for the redo helper to exit on pipe EOF; do not blindly signal it.

Crash recovery currently replaces the bounded cell. It does not promise zero
interruption when the daemon or supervisor dies. Adding/suspending/waking/deleting
computes through the live supervisor preserves storage PIDs. Normal compute stop
signals the verified Postgres postmaster and waits for its shutdown checkpoint
before terminating compute_ctl. All ownership evidence survives abrupt daemon loss.

## Storage and authentication

The local profile starts Process Compose plus four storage processes: SeaweedFS
in combined-server mode, storage broker, one safekeeper, and pageserver. Each compute
adds compute_ctl and its PostgreSQL children. SeaweedFS internally runs its master,
volume, filer and S3 services in the combined process. There is no controller,
controller Postgres or notification sink in this single-owner profile. The daemon
serves a narrow authenticated `/validate` callback so the pageserver deletion
queue can check tenant ownership and generation before reclaiming S3 layers.

All TCP listeners use generated, persisted loopback ports. The data root is private
and exclusively locked; credentials and generated configuration are private files.
Storage HTTP and PostgreSQL protocols use separate Ed25519 JWT scopes. S3 uses
per-installation access/secret keys. Compute management uses authenticated JWKS.
The broker and engine-internal HTTP APIs are loopback-only; this is a trusted
single-user local profile, not a hostile multiuser-host isolation boundary.
SeaweedFS also creates private, port-named gRPC sockets under `/tmp` in its upstream
combined-server implementation. Process creation inherits umask 077.

The base backup's localhost trust rules are unsuitable for a local SQL listener.
A separate native `hba_file` requires a password on TCP from initial startup.
compute_ctl bootstraps `cloud_admin` through a private Unix socket under the data
root. Long data roots that exceed the OS socket-path limit fail explicitly.
The qualified E01 bundle does not ship `pg_stat_statements`; native computes preload
only `neon`, without attempting a cloud extension download. Telemetry export is
disabled for the native processes.

The direct pageserver adapter uses `PUT /v1/tenant/{id}/location_config` with an
explicit generation and `POST /v1/tenant/{id}/timeline` for PG17. It is separate
from the operator's controller API. Local emergency mode is bounded by the OS
single-owner fence; it must never be reused for a multi-owner/cloud deployment.

## Why this SeaweedFS configuration

Pinned upstream source: [SeaweedFS 4.45](https://github.com/seaweedfs/seaweedfs/tree/79b87202136cebdaaa7db4d94eaa5915ad381276).

- The default [LevelDB2 store](https://github.com/seaweedfs/seaweedfs/blob/79b87202136cebdaaa7db4d94eaa5915ad381276/weed/filer/leveldb2/leveldb2_store.go)
  calls `Put`/`Delete` with nil write options, which do not request a synchronous
  metadata commit. We do not use it for the native cell.
- The existing [SQLite store](https://github.com/seaweedfs/seaweedfs/blob/79b87202136cebdaaa7db4d94eaa5915ad381276/weed/filer/sqlite/sqlite_store.go)
  accepts a SQLite URI. The cell explicitly selects WAL and synchronous FULL for
  every connection. Linux uses the checksum-pinned upstream `full` release;
  macOS builds unmodified upstream source with its `sqlite` build tag. These
  differ from P00's default SeaweedFS archives, which remain baseline probes.
- Filer storage policy is persisted **inside the filer** at
  `/etc/seaweedfs/filer.conf`, with fsync enabled for `/buckets/`. A file beside
  `filer.toml` would not configure this policy. The [volume write path](https://github.com/seaweedfs/seaweedfs/blob/79b87202136cebdaaa7db4d94eaa5915ad381276/weed/storage/volume_write.go)
  propagates data sync errors rather than acknowledging the failed write.
- The old goraft implementation failed single-master recovery in the P03 test
  after abrupt shutdown (persisted membership was empty and it did not elect a
  leader). The shipped HashiCorp Raft option passed that same test, so the native
  profile explicitly selects it. No object-store storage code was rewritten.

The S3 suite includes multipart operations even though the pinned Neon upload
adapter currently uses PutObject for its layer files. Cold restore waits for the
pageserver remote-consistent LSN before deleting disposable engine files in a
fresh test root. It then reconstructs the database from actual S3 objects.

These tests establish compatibility and observed crash recovery, not a general
power-loss guarantee. SeaweedFS's volume index, directory creation, deletion and
compaction paths still require filesystem fault testing. P03 does not promote
these engineering artifacts to a publicly qualified installer or change the
existing PG17 minor-version/signing/licensing release gates.

Safekeeper WAL is durable state, not an ordinary cache. The runtime preserves it
on shutdown and crash recovery. Only the isolated cold-restore test removes it,
after proving the fixture LSN is already durable in the object-store-backed
pageserver image. Never apply that test cleanup procedure to a working cell.
