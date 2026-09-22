# UC09.6 — Revocation, audit and governed recovery

Schema 21 finishes the private governed control path. Shared network ingress is
still disabled; the console workflow and installed-release gate remain UC09.7–.8.
The existing local-owner profile remains available on Linux and macOS.

## Admission and revocation

Every authenticated request checks current platform identity and policy. OIDC
requests introspect the configured on-prem provider; neither an old JWT signature
nor an external administrator label supplies authority. Authentication workers
have a 20-second monotonic commit deadline. A successful check supplies at most
30 seconds of provider freshness, measured from the start of authentication.
A negative response or provider outage immediately clears that session's
freshness. Local logout remains possible during provider failure.

Runtime renewals, catalog results, PG commits, clone activation, exports and
publication recheck the originating session and current policy. Provider-backed
background exports therefore need an authenticated heartbeat within 30 seconds.
They fail closed if the initiating client disappears. Service credentials use
platform revocation and their explicit expiry. Provider sessions from schema 20
start without freshness and must authenticate again.

The isolated runtime retains its independent 30-second monotonic watchdog.
Dropping its channel, missing renewal, clock failure, policy denial or full audit
closes the whole sandbox, including kernels, Sail, cached data and open file
descriptors. A catalog worker cannot commit a result older than 25 seconds.
PG statements have a two-second deadline and transactions a ten-second server
limit; role cleanup terminates remaining connections. Results are bounded single
responses; shared WebSockets and legacy result-stream routes remain closed.

Platform disable/revoke is acknowledged only after the policy/session transaction
and audit commit. New admissions then fail; the active-work target is 60 seconds
from acknowledgement. IdP disable is a separate target of five minutes. The
qualified Keycloak provider must report account disable through introspection;
a provider that merely validates an otherwise live token does not qualify.
At the platform boundary a missed provider heartbeat closes authority in 30
seconds, and an independently running sandbox expires within another 30 seconds.

Wall time is checked against a process monotonic anchor with a five-second skew
allowance, and against a durable high-water mark recorded on credential issue
and authentication. A clock step outside that allowance closes governed work;
it cannot refresh a lease. Correct the clock before retrying. No clock check
turns a previously expired execution lease back into a live lease.

## Audit operations

A unified `security_audit` stream captures identity, project policy, catalog,
runtime and governed data events. Metadata includes actor/effective principal,
realm/project, policy revision, immutable source and publication/table revisions,
PG MVCC snapshot and transaction ID, request digest, operation ID and outcome
where applicable. SQL uses repeatable-read transactions so its recorded snapshot
identifies the view used by the statement. SQL text, notebook contents, result
rows, archives, bearer tokens and provider secrets are excluded. Authenticated
denials and crash recovery outcomes are recorded without exception text.

Retention is explicit: at most 10,000 unacknowledged events, each at most 8 KiB.
The three compatibility audit feeds retain their most recent 1,000 events each.
The unified stream stops new governed admission when full; it never silently
rotates away an unexported event. SQLite reuses released pages; export does not
promise filesystem compaction. Historical feeds are imported on upgrade. An
installation with more than the admitted audit capacity must archive and reduce
its historical audit under stopped operator maintenance before upgrading; the
backed-up migration fails atomically instead of discarding history.

Use private operator request files with `supabricks identity admin --request-file
REQUEST --data-dir ROOT`:

```json
{"action":"audit_export","after":0}
```

This returns up to 200 events / 24 KiB of event bodies, an inclusive `through`
cursor and a SHA-256 of the returned event array. Save the complete response in a
private archive, fsync it (or confirm receipt in the operator's external archive),
then acknowledge the exact page:

```json
{"action":"audit_acknowledge","after":0,"through":200,"sha256":"returned digest"}
```

Only the oldest retained page can be released. Wrong digests, skipped pages and
changed pages are refused. Continue from the returned cursor; sequence numbers
are never reused. The platform verifies the page, not the durability of an
external archive: the operator owns that responsibility. No archive is uploaded
automatically. Export and acknowledgement remain available while admission is
closed. Exported files contain security metadata and need administrative access.

A failed audit write rolls back security mutations and refuses new work. An
already committed PG transaction whose result cannot be journaled is marked
uncertain, never replayed. Process cleanup and independent expiry do not depend
on writing an audit event. A logically full audit still permits closed startup:
recovery is counted durably and the deferred recovery summary is appended after
a page is acknowledged. Actual filesystem exhaustion may require freeing space
before the daemon can open its journal. It does not extend sandbox or PG leases.

## Backup and restore

A backup containing remote principals, provider configuration or governed branches
is an administrative asset, even while shared ingress is disabled. Backups may
contain historical credentials and user data; protect the entire bundle. Restore
still requires a fresh destination and the exact source release/target.

Governed restore runs under the durable `restore-incomplete` guard and:

- closes authenticated admission in SQLite;
- removes sessions, pending logins, provider secrets, group memberships and all
  historical project, execution, catalog and PG grants;
- disables remote principals, clears bootstrap authority and old grant plans,
  fences policy revisions and cancels historical execution/result authority;
- preserves data and identity records for explicit reconciliation;
- rotates PG control/application passwords, storage/compute signing keys, S3,
  supervisor and validation credentials; the managed catalog rotates its keys
  through its existing restartable protocol;
- enrolls restored branches so copied non-control PG logins are retired before
  governed SQL is admitted.

Inspect `{"action":"restore_status"}`. Configure the current IdP, review the
current issuer/subject mappings, explicitly enable the identities that should
exist, and issue new project/data/execute grants. Reconcile the managed UC grant
plan against current remote state; existing UC mappings prevent opening a restore
until that reconciliation is ready. Then submit:

```json
{"action":"restore_reconcile","restore_id":"returned restore ID","realm_id":"returned source realm ID"}
```

This final operator action checks the restore and realm IDs, records the decision,
rotates sessions again and opens admission under the newly reviewed policy.
Issue new credentials afterwards. Omitting regrants leaves access denied. A
backup predating a revoke cannot automatically resurrect that permission.

This restore preserves the source realm. A different realm ID is rejected;
cross-realm activation is not an admitted import path and requires a separate
explicit identity-remapping workflow. Never relabel the realm or copy historical
grants to simulate remapping. Ordinary `.sbdata` transfers remain the supported
way to move data into an independently administered realm without its identities.
Local-owner-only backups preserve their original profile and credentials.

## Evidence and trust boundary

Portable tests cover audit capacity, export acknowledgement, SQLite disk full,
clock rollback, failed recovery, schema-20 upgrade and backup rollback after
revocation. The native governed harness exercises real PG revocation, daemon
loss, restored password rejection, storage-key rotation and newly granted access.
The identity harness records IdP acknowledgement and observed closure separately.
The real UC/gVisor test exercises open descriptors, cached bytes and in-flight
Sail work under renewal loss, audit failure and platform disable. Reports include
last observed successful access, acknowledgement and closure times.

The host administrator remains trusted: root can replace binaries, change the
clock, edit SQLite, restore old disk images, read backups or bypass private
sockets. The local audit is not tamper-resistant against that administrator.
External append-only archival and protection against malicious host operators
are separate capabilities. UC09.8 must qualify the exact installed archive;
these source-level checks do not enable shared ingress.
