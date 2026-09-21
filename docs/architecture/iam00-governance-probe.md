# IAM00: identity and execution isolation feasibility

Status: implemented developer probe; Linux evidence recorded below (2026-09-21). UC09 governed
product mode remains disabled. This is UC09.0, built on UC08's exact qualified
alpha.34 archive and the [Linux-first UC09 scope](uc09-governed-on-prem.md).

## Decision

Proceed with **stable platform identities, a private UC principal broker,
materialized direct UC grants for platform groups, and one OCI/gVisor sandbox per
execution lease**. Continue UC09.1 identity/session work. Do not enable shared
network ingress, trust external email as a UC identity, or call the current
local-owner runtime a multi-user system.

The reproducible [harness](../../e2e/native/iam/README.md),
[component pins](../../e2e/native/iam/pins.lock.json),
[capability evidence](iam00-evidence/linux-x86_64.json), and
[validator](../../e2e/native/iam/qualify.py) are the acceptance artifacts. The
separate `iam-probe` CI job reruns the real Linux experiment; it does not replace
UC08 release qualification or qualify a newly built product archive.

## Identity findings

The probe uses real Keycloak authorization-code/PKCE sessions for Alice, Bob and
an equal-email user, plus a client-credentials service identity. A persisted
`(issuer, subject) -> opaque UUID` mapping survives reopening and email changes;
deleting/recreating a subject creates a different principal with no inherited UC
grants. Exact issuer/audience checks and signature verification reject tampering.
Email and external labels do not choose permissions. The prototype is test code;
UC09.1 should use a maintained OIDC implementation, not ship this minimal JWT parser.

The pinned UC fork **does not qualify for direct external federation**:

- Two independently signed Keycloak subjects with the same email exchange into
  the same UC subject.
- A correctly signed external `email=admin` claim exchanges into a credential
  that can create a catalog. This is a demonstrated reserved-name collision,
  not an assertion that unsigned or wrong-issuer tokens bypass signature checks.
- The SCIM group route has no usable service and returns HTTP 500 (`Couldn't
  unwrap service.`). Native groups are not a usable foundation at this pin.

These findings concern the proposed external federation path. The installed
local-owner profile trusts its own internal issuer; IAM00 does not add Keycloak
to an installed cell's issuer allowlist.

The probe's private broker maps principal UUIDs into collision-free internal UC
subjects (`p-<uuid>@iam00.invalid`). Non-admin UC credentials filter catalogs and
reject unauthorized table metadata and actual Sail table reads. Equal-email,
service and recreated principals receive no Alice grants. The broker's `admin`
label test resolves to Bob and has Bob's rights. Bootstrap UC credentials and
signing material remain in the trusted control plane.

UC09.3 must restrict UC's trusted issuer to the private broker and keep its API
inaccessible to users/workloads. Platform groups will materialize direct grants,
with an origin journal and overlap-aware reconciliation; UC remains the effective
grant authority. Before supporting direct federation, the UC fork needs a reviewed
`(issuer, subject)` identity path and explicit rejection of externally selected
reserved principals. That path is **no-go** at this pin. No UC source patch is
needed to prove the private internal-subject adapter; no new fork pin is claimed.

Keycloak introspection detects an administratively disabled user while their JWT
is still unexpired. Admission also fails during a real paused-provider outage,
then resumes only after active introspection. Introspection's client must be in
the token audience at Keycloak 26.7.4; the fixture grants that explicit audience.
A cached JWT alone is not a disable/revocation mechanism. The measured detection
latency is a feasibility observation, not a complete platform session guarantee.

## Execution findings and trust boundary

Each of two concurrent users runs the exact archived Jupyter server, bounded
kernel and bootstrap with source-built Sail inside gVisor. A disposable, one-use
launch shim replaces the future governed admission adapter. Kernel code queries
real 10/100 MiB Delta payloads and exercises NumPy, pandas, Arrow, delta-rs,
gRPC and ZeroMQ. This tests runtime compatibility and native code isolation;
console login, the governed daemon adapter and package-build isolation remain
later UC09 slices.

The sandbox has UID 1000, no capabilities, no-new-privileges, a read-only product
and admitted data mount, private process and network namespaces, and bounded
scratch. Direct paths, another principal's storage, credential/cache canaries,
control/Docker sockets, host processes, UC/PG backend ports, raw network sockets
and host privilege escalation are denied. Positive controls demonstrate admitted
data reads, real UC/PG listeners, live managed processes and bounded scratch
allocation. Direct Sail access to an unmounted Delta path fails. Any admitted
bytes remain readable to that execution until termination; this is not DLP.

The developer topology uses a separate privileged **trusted launcher container**
per execution to run nested gVisor without reconfiguring the host Docker daemon.
Untrusted notebook code runs only inside gVisor. The outer container has no network
or Docker socket. It enforces the per-execution cgroup; the nested runtime ignores
only the outer container's read-only cgroup interface. This is a reproducible
feasibility setup, not the production deployment specification. UC09.4 must wire
an operator-owned supervisor/runtime with equivalent restrictions, qualification
and failure handling. Host root/kernel and launcher compromise remain outside
this boundary. No host fallback is allowed if runsc fails.

Ubuntu 24.04 is the chosen rootfs. The exact Sail binary requires GLIBC_2.38;
Debian Bookworm's glibc did not satisfy that requirement (rejected rootfs
`debian@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251`). Minimal Ubuntu also
needs timezone data: the harness copies the archive's pinned UTC zonefile and
writes `Etc/UTC`. Both requirements are reproduced in the controller. The full
pinned gVisor distribution includes its sidecar binaries and uses strict sidecar
matching; a lone `runsc` download is insufficient for this release.

No Sail source patch was required for the demonstrated path. The admitted-data
adapter must continue resolving/grant-checking metadata outside the workload,
mounting only the approved immutable file closure, and using private per-lease
Sail sessions. External Spark clients, shared Sail sessions, arbitrary storage
and filesystem pass-through remain no-go.

## PG and revocation

Two actual pinned Neon/PG17 branch computes exercise an ordinary restricted login.
The allowed table is readable; other tables/branches, owner/control logins and
roles, server-file reads, COPY PROGRAM, superuser/role creation, replication,
BYPASSRLS and broad-reader grants are denied. An active `pg_sleep` is terminated
by the control plane. Independent fresh branches are tested; clone/import role
reset and RLS-aware copy authorization remain UC09.5 acceptance work.

Revocation kills the entire sandbox while the kernel runs a long Sail query and
a separate thread repeatedly reads an already-open Parquet descriptor. The probe
waits for the managed kernel to report busy and for Sail CPU time to advance
before requesting revocation. The
heartbeat stops and runsc state becomes empty. In the daemon-loss case, a separate
trusted process actually renews a two-second monotonic lease; killing that issuer
stops renewals and the independent watchdog kills the workload after expiry.
Stopping only the browser, expiring a token, or changing a file's permissions is
not the tested mechanism.

The chosen product target remains **60 seconds for execution revocation** and
**five minutes for IdP disable**, pending integrated acceptance. Proposed policy:
renew only with fresh identity/policy authorization, cap execution leases at 60
seconds, check IdP active state at least every 60 seconds, and stop renewing on
provider/control-plane failure. That composition leaves margin inside five
minutes; UC09.6 must prove the complete implementation including races, restart,
results/spills, restore, and UC/PG connections. IAM00's accelerated two-second
lease proves feasibility, not the final 60-second product implementation.

## Resource envelope

The committed report records individual observations, not statistically robust
benchmarks. Both users run concurrently; each gets **2 CPUs, 2 GiB RAM, no swap,
512 tasks, 512 MiB scratch**, plus 64 MiB `/tmp` and 64 MiB `/dev/shm` tmpfs mounts
charged to its memory cgroup. An allocation beyond scratch capacity fails with
ENOSPC. Sail has a 256 MiB memory pool and 256 MiB spill budget within that envelope.
Notebook startup, idle process RSS, full 10/100 MiB payload reads, post-read RSS
and enclosing cgroup peak/current memory are recorded. RSS sums double-count
shared mappings; cgroup memory is the capacity-planning measure. Reads may use
host page cache and are not cold-disk throughput measurements.

| Measured observation | Alice | Bob |
| --- | --- | --- |
| Notebook startup | 6.965 s | 6.776 s |
| Enclosing cgroup peak | 1013.0 MiB | 969.9 MiB |
| 10 MiB Delta read | 0.048 s | 0.048 s |
| 100 MiB Delta read | 0.125 s | 0.124 s |
| Execution stop | 0.125 s (revoke) | 2.085 s (issuer loss/expiry) |

IdP-disable detection took 0.059 seconds; PG query termination took 0.008 seconds. The complete local run passed 123 capability checks with all cleanup checks passing.

Initial governed qualification target: **two concurrent executions on a dedicated
4-core/16-GiB Linux server**, using the per-lease limits above and reserving the
remaining memory for PG/storage, UC, Keycloak, the control plane and OS. This is
an explicit proposed support envelope derived from the two-user probe, not a
claim that arbitrary packages or larger datasets fit. Exceeding it should queue
or reject admission until more concurrency is qualified. Quota/OOM handling,
large/hostile packages and end-user diagnostics must be completed in UC09.4.

## Qualification boundaries and next work

IAM00 is a go for implementation of the selected architecture, with the explicit
no-go paths above. It is not a security audit or governed release certification.
UC09.1 starts stable identity/session storage and real on-prem login; UC09.2–.8
still supply authorization, group-grant reconciliation, production isolation,
PG/data movement, revocation/audit/recovery, console administration and exact
release qualification. No local-owner migration or public listener changed.

The harness verifies cleanup of owned native processes, containers and sandbox
runtime state. Logs and test secrets stay in private disposable directories;
only bounded capability facts are published. CI rejects incomplete evidence,
stale source, changed pins, missing negative checks, false governance claims,
failed cleanup and missed revocation/resource bounds.

## Source basis

- [UC AuthService at the reviewed fork pin](https://github.com/supabricks/unitycatalog/blob/8e195426ce03e593b03c92f87051d7bf013aeee1/server/src/main/java/io/unitycatalog/server/service/AuthService.java)
  and its SecurityContext implementation; behavior above is additionally tested.
- [OIDC subject stability](https://openid.net/specs/openid-connect-core-1_0.html#ClaimStability)
  and [Keycloak endpoints](https://www.keycloak.org/securing-apps/oidc-layers).
- [Keycloak 26.7.4 introspection checks](https://github.com/keycloak/keycloak/blob/26.7.4/services/src/main/java/org/keycloak/protocol/oidc/AccessTokenIntrospectionProvider.java).
- [gVisor OCI setup](https://gvisor.dev/docs/user_guide/quick_start/oci/),
  [distribution/sidecars](https://gvisor.dev/docs/user_guide/install/) and
  [security boundaries](https://gvisor.dev/docs/architecture_guide/security/).
