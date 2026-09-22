# UC09.8 — Installed governed release qualification

UC09.7 is merged in platform #73 and console #10. UC09.8 adds a candidate
alpha.35 archive and a Linux governed qualification gate to the existing R04
matrix. Implementation is not a qualified release: UC09 remains incomplete until
all inherited Linux/macOS and governed reports pass for their exact archives.

## Installed boundary

The explicit `console --governed --ingress` configuration terminates TLS and
forwards only to the private governed Unix socket. The existing typed routes,
OIDC/PKCE, Host/Origin/CSRF checks and live authorization remain authoritative;
legacy console and operator endpoints remain inaccessible. Secure cookies,
32 connection slots, five-second handshakes, bounded bytes and connection
lifetimes constrain ingress. Only Linux x86_64 is supported. Loopback TLS may be
used for qualification. A network-facing listener requires a private,
operator-reviewed R04 receipt bound to the fully verified installed inventory.
The receipt is a deployment record, not a cryptographic publisher signature;
the OS/Docker owner remains trusted.

Execution accepts the verified inventory of its own installed candidate while
retaining the historical alpha.34 pin for source probes. The packaged runtime
preparation tool verifies reviewed gVisor input, exports the already-staged rootfs
image without pulling, and writes a private configuration bound to that archive.
The installed [server guide](../handbook/governed-server.md) covers setup,
administration, the two-user/service demonstration, limits and recovery.

## Exact archive gate

`install/native/qualify_governed.py` runs the real signed curl installer inside a
network-disabled Docker controller. Keycloak joins that isolated namespace over
TLS; native services and workload inputs come from the installed archive. Four
real suites cover identity/disable/outage, governed PG/data/grant races/audit
failure and restore, the named backend upgrade, and the TLS browser workflow.
The browser runs two gVisor notebooks concurrently, tests third-execution denial,
private mounts/network/capabilities and scratch ENOSPC, measures cgroup memory,
and revokes a service execution while a separate project's peer continues.
Project policy revisions intentionally fence all executions in that project.

Reports bind platform/console/UC/Sail provenance, archive and installed binary,
console inventory, execution configuration, pinned IdP/rootfs/gVisor and workload
source hashes. Each suite has a bounded timeout and descendant cleanup census;
the outer controller also inventories and removes owned leftover containers.
Private diagnostic roots retain failed attempts. `--slice` is development-only:
its partial reports cannot qualify a release.

The collector requires the nominal dedicated 4-CPU/16-GiB host envelope (14–16
GiB usable RAM), plus two measured executions within the pinned per-lease quotas.
Controller limits alone cannot prove aggregate host capacity because Docker
siblings execute outside its cgroup. A larger workstation run provides debugging
evidence only. Actual performance observations are not throughput guarantees.

The R04 collector first requires every existing local-owner Linux/macOS gate,
then validates the Linux governed report and emits the exact-archive ingress
receipt. Missing checks, mixed archives, fallback execution runtimes, missing TLS,
resource/revocation failures and cleanup leaks fail closed. macOS gains no shared
profile. No receipt or qualified-release claim is issued from source tests.

## Named UC backend update

Only alpha.34's UC server commit `8e195426ce03e593b03c92f87051d7bf013aeee1`
to `17280eababcc4ae31c53522d930ef2fe92d1c331` is admitted. Every JRE, H2,
dependency and configuration file must remain identical; only the server JAR and
build provenance may change. Native engine/analytical compatibility checks stay
in force. The previous installed binary creates the stopped H2/SQLite checkpoint;
the candidate verifies its identity and hashes under the data-root lock before
creating the durable upgrade journal. The new backend validates retained
publications before switching the runtime/current link and rotates UC credentials.
The original backup remains restorable with its original binary. Unknown backend
or dependency changes remain rejected.

## Validation record

Portable Rust, TLS ingress, named-transition unit tests and native evidence
collector tests are exercised locally. Installed archive checks are development
runs until the complete CI matrix has passed. Failed attempts exposed fixture-only
runtime overrides, overlong PostgreSQL socket paths, audit-capacity filling and
branch restart assumptions; retain these diagnostics rather than counting retries
as successful qualification. Final PR validation records identify the completed
runs and any still-pending exact-archive evidence.
