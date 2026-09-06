# P04 native databases and branches

The local daemon now executes database and branch operations against the pinned
PG17.8 engine. SQLite schema 4 retains project/branch/endpoint identities, exact
ancestor positions, credentials, default designation, expiry and operation
progress. All operations run in the existing single-owner storage cell.

## Engineering API

Use newline-delimited JSON on the private `control.sock`. Each request has
`{"version":1,"request":{...}}`; responses contain `result` or `error`. Project and
branch IDs are UUIDs. Register a project using the existing `register_project`
request before submitting operations. Names are scoped to the project.

A mutation request has `method: "submit"`, `project_id`, a caller-chosen `key`,
and `mutation`. Retrying an identical key and payload returns the same operation;
changing the payload conflicts. The result includes an operation ID and branch
ID. Poll `{"method":"operation","id":"<operation UUID>"}` until `succeeded`,
`failed` or `superseded`. Pending failures carry a sanitized diagnostic and
`retryable`; they do not prevent other operations from progressing.

| Mutation kind | Fields beyond `kind` | Behavior |
|---|---|---|
| `create_database` | `name`, `ports` | Create an independent PG17 root timeline; the first becomes the project default |
| `branch_from` | `name`, `parent_id`, `ports`, optional `point`, optional `timeout_ms` | Create a child at an exact parent position |
| `set_state` | `branch_id`, `expected_revision`, `desired` | Run, suspend or delete (`running`, `suspended`, `deleted`) |
| `set_ttl` | `branch_id`, `expected_revision`, `expires_at_ms` | Set a future Unix millisecond deadline, or clear with null |
| `set_default` | `branch_id` | Designate a live branch without a TTL as project default |
| `force_delete` | `branch_id`, `expected_revision` | Explicitly cancel work leases and permit deleting the default |

`ports` supplies distinct available loopback `sql`, `external_http` and
`internal_http` ports. Storage ports cannot be reused. Port selection and stable
connection routing will move behind the public interface in P05/P06. The legacy
`create_branch` request remains compatible; root branches made with it do not
implicitly become defaults.

`point` is `{"kind":"head"}` (the default), `{"kind":"lsn","lsn":"0/2000000"}`,
or `{"kind":"time","timestamp":"2026-09-05T12:00:00Z"}`. RFC3339 offsets are
normalized to UTC for the engine. `timeout_ms` bounds capture and ingestion,
defaults to 90000, and must be between 1000 and 300000. Unavailable history and
future positions fail; no earlier point is substituted. Eight-byte alignment is
necessary for an explicit LSN, but does not by itself establish a valid retained
WAL position.

Read requests use `project_id`: `list_branches` (optional `include_deleted`),
`get_branch` with `id`, and `connection` with `id`. Connection results include
`host`, `port`, `database`, `username` and `password`, and are returned only for a
live, configured running compute accepting work. They contain secrets and must
not be logged. P04 connects directly to the compute; stable addresses and wake on
connection belong to P05.

`acquire_lease` takes `project_id`, `branch_id`, `holder`, `ttl_ms`.
`renew_lease` takes `project_id`, the returned `lease`, and `ttl_ms`;
`release_lease` takes `project_id` and `lease`. Existing work can renew while an
expired branch drains. New leases and connection requests are rejected.

## Exact boundaries and replay

For head branching, a private control connection captures
`pg_current_wal_flush_lsn()` from the parent. The daemon commits that exact LSN to
SQLite before proceeding, waits until the pageserver has ingested it, and takes
an engine LSN lease before creating the timeline. A retry uses the recorded LSN.
Timestamp requests resolve through the pageserver's retained commit history and
then use the same exact-position path. Branches of branches use their own parent
and may not precede its ancestor position.

A suspended parent wakes under a durable operation-owned pin. Parent lifecycle
changes conflict until the new timeline is durable. The daemon restores
suspension after releasing the pin, provided no later lifecycle revision or
protected work supersedes that restoration. A crash cannot silently expire the
internal pin while creation is replayable.

A successful idempotent timeline POST is the creation durability barrier. A GET
can observe an incomplete creation, so it is insufficient to checkpoint success.
After that checkpoint, a missing timeline is treated as unavailable data, never
as permission to initialize an empty replacement. The engine ancestry must match
the persisted tenant, timeline and ancestor LSN.

## Credentials, expiry and deletion

Each endpoint has independently persisted random application and control
credentials. The application role `supabricks_owner` owns the `postgres` database
and can manage its schema. It has no superuser, role creation, database creation,
replication, bypass-RLS or `neon_superuser` privileges. `cloud_admin` is private
control state used for provisioning, flush capture and draining. Control SQL is
bounded and avoids logging password statements. The compute template does not
provision the application role through Neon's broad administrative role helper.
Branch creation rotates both credential sets independently of the parent.

Expiry is sticky. The API immediately rejects new work at the deadline; the
reconciler disables application login and waits for existing application SQL
sessions and work leases to finish. It then journals ordinary deletion. This is
bounded by reconciliation availability, not a wire-level deadline enforced by a
gateway. Existing lease holders can prolong their work. Explicit force deletion
cancels leases and stops SQL instead of draining it.

Ordinary deletion protects the project default and leased work. All deletion,
including force, rejects parents with children not fully deleted or active
branch operations. Teardown revokes/stops the compute, deletes the pageserver
and safekeeper timelines, removes local compute/socket/spec files, and finally
releases ports, credentials and worktree selections. Every step is replayable.
Before interpreting a missing timeline, teardown reattaches its tenant from S3;
losing the pageserver cache must not skip remote cleanup. Branch records and
operation receipts remain for auditing and idempotency.

The pageserver's queued S3 deletions require a generation-validation callback.
The daemon provides only authenticated `POST /validate` on a private persisted
loopback port. It approves known tenant IDs at its current fenced ownership
generation and rejects unknown tenants and stale generations. This is not a full
controller service. Runtime configuration version 1 upgrades atomically to
version 2 by adding this port and a separate token, preserving existing ports
and secrets. The callback remains available during ordered shutdown.

## Recovery boundary

Compute restart and cell-owner recovery preserve SQLite, credentials, storage
keys, safekeeper WAL and object storage. Losing SQLite is a different failure:
startup refuses to initialize new metadata if engine state remains. An empty
pre-mounted object directory is allowed for a fresh installation. Restore a
complete, consistent stopped-cell backup containing metadata, keys and storage;
S3 layers alone cannot reconstruct identities, credentials, leases or selections.
The native test restores the original stopped SQLite file while all its matching
storage remains intact. A coordinated recovery/export tool and power-loss
qualification remain R03 work.
