# P05 stable connections and suspension

Each live branch has a persisted loopback listener separate from its private
compute ports. The engineering `connection` request returns the same address,
credentials and ordinary PostgreSQL URI while compute is running, suspended,
waking, or being recreated after owner loss. It does not itself wake compute.
Opening a TCP connection acquires a connection lease and wakes compute when
needed. Expired or deleted branches reject new work.

## Listener ownership and routing

The daemon binds a socket before recording a new listener port in SQLite schema
5. It excludes persisted compute, storage and listener reservations when selecting
ports. Restart binds the same address; an unavailable address reports a conflict
in `daemon.log` and prevents startup. It never chooses a replacement port for an
existing branch. `SO_REUSEADDR` permits restart after TCP TIME_WAIT;
`SO_REUSEPORT` is not enabled. Deleting a branch closes its listener and retains
the reservation until ordered cleanup completes.

Existing P04 compute ports remain private engine endpoints. The P05 public
listener is allocated separately. Applications should obtain `connection.uri`
once and use it through subsequent suspends and wakes; internal compute-control
and export code continues using the independently authenticated private path.
The public database/connection CLI and MCP commands remain P06 work.

Accept and lifecycle decisions run in the single SQLite writer. Socket-owned
leases are separate from renewable work leases and have no wall-clock expiry:
an idle pooled connection still requires compute. Closing the socket releases
its lease. Owner recovery fences surviving processes and discards the old
owner's connection leases because its TCP sessions cannot survive the daemon.
Work leases retain their existing expiry and generation rules.

The gateway waits for the matching compute revision, process identity and
successful authenticated SQL provisioning before connecting its backend. All
concurrent accepts share one journaled wake. A later `running` request for an
already converged running branch is a no-op, preserving active connections.
A lifecycle request cannot supersede an unfinished suspend with a new running
revision; clients wait for retirement, then wake automatically.

A single Tokio I/O worker buffers startup bytes and copies streams. PostgreSQL
owns authentication, TLS, query execution, prepared statements, COPY and cancel
requests. The gateway neither parses SQL nor pools or multiplexes sessions.
A client half-close still receives the remaining backend response. Backend EOF
closes the relay and releases its lease, including when an idle pool has not yet
noticed a failed authentication or terminated backend. A replaced compute also
invalidates relays associated with its old process identity.

## Limits and TLS

The gateway admits at most 256 simultaneous connections per cell and 64 per
branch, including unauthenticated clients and clients waiting for wake. It
buffers at most 64 KiB of client startup data per waiting connection. Overflow
closes the new socket. The default wake/backend-connect deadline is 30 seconds;
the private `runtime.json` field `connection_startup_timeout_ms` accepts 100 to
120000 milliseconds. Timeout or disconnect cancels that waiter and releases its
lease; other clients' wakes continue. Authentication deadlines after handoff
remain PostgreSQL's responsibility. Status reports listener/connection counts,
limits and listener errors under `gateway` without exposing credentials.

The default trusted local profile uses PostgreSQL's normal SSL negotiation with
TLS disabled. To enable PostgreSQL TLS, stop the cell and configure existing,
absolute certificate/key paths in private `runtime.json`:

```json
"compute_tls": {
  "certificate": "/private/persistent/path/server.crt",
  "key": "/private/persistent/path/server.key"
}
```

Keep this material outside disposable compute directories; PostgreSQL requires
appropriate private-key permissions. The certificate must cover the client's
host, normally `127.0.0.1`. Client `sslmode=verify-full` and trust configuration
are passed directly to PostgreSQL. The relay does not terminate TLS or manage a
certificate authority. TLS settings persist across fresh-directory wake. This
is a trusted single-user loopback deployment, not a multiuser-host sandbox.

## Safe suspension and recovery

Suspension rejects active client leases, internal work leases and protected
branch-creation pins. A connection accepted first prevents suspension. If a
suspend was already committed, a new connection gets a lease for the next wake
and waits for the current suspend to finish. Expiry rejects new accepts and
allows established SQL and internal work to drain; pending startup waiters close.
Explicit `force_delete` closes active sockets and cancels protected work rather
than draining it. Parent/default branch protections remain P04 behavior.

A suspension journals three replayable steps:

1. Request authenticated compute termination, persist its final flush LSN and
   revision, and wait for the pageserver to ingest that boundary.
2. Revoke launch authorization and stop the verified compute process group.
3. Remove disposable compute, socket and generated-spec files.

The observed suspended revision advances only after retirement. Tenant/timeline
identity, application/control credentials and the public listener remain intact.
The next wake obtains a fresh compute directory. Migration preserves the stop
checkpoint of an in-flight P04 suspend and appends boundary capture and
retirement. Branches already suspended by P04 retire their old directory before
their first P05 wake. If the owner dies before the
termination receipt is recorded, recovery first fences the old process group,
then obtains the durable commit boundary from the safekeeper. An old receipt
cannot stand in for a later suspend revision. Unavailable storage leaves the
operation pending instead of inventing a boundary or deleting uncertain state.

Suspension is explicit in this slice; there is no automatic idle timer. Keeping
a pool connected intentionally keeps compute active. Whole-cell `down` closes
connections and stops services while retaining desired branch state and durable
storage; it is distinct from branch suspension. R03 still owns coordinated
backup tooling and power-loss qualification.

The implementation follows PostgreSQL's [protocol message flow](https://www.postgresql.org/docs/17/protocol-flow.html).
[Native qualification](../../e2e/native/README.md) runs ordinary psql, psycopg and
Node pg clients, including TLS and cancellation, on Linux and macOS.
