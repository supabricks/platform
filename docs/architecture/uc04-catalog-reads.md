# UC04: session-pinned catalog reads

Catalog reads are opt-in on CLI/MCP analytics opens, Spark shells, console SQL
workspace opens and notebook creation. A project/deployment binding owns the
request. Opening by branch resolves its complete published catalog head once;
an explicit epoch selects that deployment's still-published revision. Request
keys replay the original session even if the head later changes.

Admission atomically journals the session, epoch, publication UUID and revision.
The existing active analytical session row is a durable lease: UC03 retirement
and snapshot GC already consult it. The lease is released only after notebook
and Sail process groups have stopped with verified process ownership. Expiry,
close, cancellation and daemon recovery share this path. No schema migration or
additional time-based publication pin is necessary.

Up to two independent resolution workers run outside the daemon writer. Each
verifies the configured managed-local provider/metastore, catalog/schema UUIDs,
and every table UUID, schema and canonical file URI against the committed set.
Requests have 750 ms transport timeouts and 64 KiB response limits; complete-set
resolution has a 15-second budget plus the final bounded request. Completion
rechecks provider identity and local snapshot identity before authorizing launch.
Failures close the session and never select another snapshot.

The frozen adapter uses a private Sail memory catalog in each existing worker
process. It creates views over fixed Delta locations with `VERSION AS OF 0`,
using both `<catalog>.<source_schema>.<source_name>` and the recorded physical UC
name. Existing `public.table` aliases continue to work. Duplicate/case-folding
collisions fail before readiness. The worker has no UC client or UC credentials;
SQL execution does not resolve mutable remote names. Sail's source pin is
unchanged; this uses the frozen-adapter option established by UC00/UC04.

Catalog provenance in `_supabricks.epoch.metadata_json`, session status and
notebook epoch metadata includes provider, metastore, namespace and table UUIDs,
publication revision and Delta versions, without credentials or file locations.
A publication refresh affects new sessions. Notebook restart uses its explicit
original epoch. Retired revisions reject new admission, including restart.

The supervisor clears the process environment. Python also scrubs inherited
SAIL, UC, UNITY, DATABRICKS and cloud-storage settings before installing explicit
memory provider configuration. No JVM plugin or ambient provider cache is used.
Each session has its own process, namespace, cache, fixed locations and bounded
lifetime (10–3600 seconds; notebooks at most 900 seconds). Sail uses IPv4
loopback on Linux and native IPv6 loopback on macOS, matching the UC00 probe
and avoiding macOS Seatbelt misclassification of IPv4-mapped gRPC sockets.

This is the managed-local, trusted-owner profile. UC is metadata authority at
admission, not a continuously consulted policy server. Existing reads can survive
an outage until their lease expires. There are no vended credentials, external
storage reads, cross-project bindings or governed user identities in UC04.
Managed SQL rejects DDL/DML; arbitrary local-owner Python and direct Spark clients
retain the existing trust boundary and are not a write-isolation security fence.

The native catalog gate stages the current binary **and current session worker**
over the pinned qualified PG/Sail/notebook baseline. It exercises console SQL,
independent Spark Connect and actual Jupyter execution, refresh and restart
isolation, provider outage, active-reader retirement, expiry and UUID replacement.
Rust/Python checks cover durable lease recovery, local-path/URI rejection,
symlinks, immutable Delta schema/version checks, alias collisions and environment
poisoning. Linux/macOS offline CI and complete alpha.30 archive qualification are
separate evidence; alpha.30 retains local control schema 15. Earlier strict
upgrade/recovery inventory failures are not bypassed by this slice.

Local Linux qualification: 229 core/local Rust tests, five Python worker tests,
seven release-evidence tests and all 32 installed catalog checks passed (27
UC01–UC03 plus five UC04 scenarios). Subsequent catalog/API tests also passed.
Cross-platform CI and full archive qualification remain pending.
