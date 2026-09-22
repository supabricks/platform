# UC09.8 — Installed governed release qualification

Status: UC09.8 is merged in [platform #74](https://github.com/supabricks/platform/pull/74),
with [console #11](https://github.com/supabricks/console/pull/11) and the audit
navigation correction in [#12](https://github.com/supabricks/console/pull/12).
Alpha.35 passed the complete exact-archive R04 matrix in
[run 35700396118](https://github.com/supabricks/platform/actions/runs/35700396118).
UC00–UC09 is complete for the planned profiles: local-owner Linux x86_64/macOS
arm64 and the explicitly qualified Linux governed shared server.

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
The installer extracts public product files with readable/executable payload
permissions for the fixed guest UID, while staging, installation ancestors and
all data/configuration stay private. This is checked independently of the host
operator UID; an owner-only payload happened to work for UID 1000 but failed
for a different server account.
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

## Qualified candidate

The [retained R04 evidence](uc098-evidence/r04-evidence.json) records every report
hash and both exact archives from [run 35700396118](https://github.com/supabricks/platform/actions/runs/35700396118).
Its [governed receipt](uc098-evidence/r04-evidence.governed.json) binds the Linux
installed inventory and the SHA-256 of those exact evidence bytes. The complete
collector passed all inherited local-owner gates before emitting that receipt.

- Archive source (tested PR merge): `c6b61a5a02f0e6a9bd9729750da7ba484e535eb8`.
- Feature head: `38a96863c7e4c2ff0917bdae33c9a3f7aefff2fd`.
- Platform #74 squash merge: `a1104e66f5d384dba9ebb135e43f6d885b890ce5`.
- Console: `4544ee6c455cd21e212efe157191df36dde6f483`.
- Sail: `9544c9253e981a82c5f9e493c43ce98a4d9d41b7`.
- Unity Catalog: `17280eababcc4ae31c53522d930ef2fe92d1c331`.
- Control schema 22; PG17.8; H2 `2.2.224`; publication manifest 1.

| Target | Archive SHA-256 | Installed inventory SHA-256 | Reports / checks |
| --- | --- | --- | --- |
| linux-x86_64 | `46288e5198ff9771b949c478eb95b0e073a8d77402489a318e5e0a64fd7fb595` | `d7558842a4576796d9e37550f40ad92608500fd083814417d473a4bf5e62b5ca` | 17 / 304 |
| macos-arm64 | `7abcf4a94ff90c45bb5ddc29b45b35e23d4cb6e18a5ba0e1240bb9f61101a4eb` | `6081d67310bd804bb71c1654d357c0aef090cf114f7066b627dd250eed1d5f5c` | 14 / 245 |

Checks are assertions across suites, not counts of distinct product features.
The Linux governed report includes all 46 required suite checks: identity 11,
data 17, named backend upgrade 4 and browser 14. The signed curl installer,
distinct installer/guest UID permissions, offline runtime preparation and
unchanged archive/inventory checks also passed. Every suite's descendant census
and the outer container census recorded zero leaks and zero remaining processes
or containers. The catalog suites independently cover 37 service and 11 browser
scenarios per target, plus 10 Linux / 9 macOS recovery scenarios.

The governed host exposed 4 CPUs and 16,766,414,848 bytes of usable RAM, within the dedicated
nominal 4-CPU/16-GiB contract. Two concurrent executions each had 2 CPUs, 2 GiB
memory without swap, 512 tasks and 512 MiB scratch. Measured cgroup peak memory
was 1,004,277,760 and 1,017,585,664 bytes. Service-execution revocation
was observed in 0.826 seconds while the independent peer continued; IdP disable
closed execution in 0.520 seconds after acknowledged denial. These are fixture measurements, not throughput
guarantees. Exact current/peak measurements and pinned IdP, rootfs, gVisor,
installer and workload hashes are retained in R04.

The follow-up completion commit changes only non-packaged architecture/plan
documentation and retained evidence. Qualification belongs to the archive source
and hashes above; a rebuild from a later main commit needs its own qualification.

## Validation and retry ledger

Portable Rust tests (289 passed, 3 ignored), TLS ingress, named-transition tests
and 56 native Python installer/evidence tests passed during implementation.
Required PR checks and both complete source catalog probes passed on the final
feature head. Console #12's delayed-response Chromium audit regression failed on
the former assets and passed with the correction on both Linux and macOS.

Earlier development attempts exposed fixture-only runtime overrides, overlong
PostgreSQL socket paths, audit-capacity filling and branch restart assumptions.
They were corrected before final acceptance. Full workstation runs provided
debugging evidence; their larger host envelope cannot qualify the shared profile.
One local assembly process exited abnormally after writing and verifying its
archive; that run was not release acceptance. Final CI assembly and qualification
completed successfully on both supported targets.

[Run 35689890677](https://github.com/supabricks/platform/actions/runs/35689890677)
failed installed identity because the installer's private umask also made public
product payloads unreadable when the installer owner differed from guest UID
1000. Public payload extraction now uses readable/executable modes beneath
private installation ancestors, with an actual signed-installer regression and
installed distinct-UID verification. A macOS source crash test also missed its
durable publication boundary; it now pauses the real provider before publication
and resumes it after killing the writer. Both source and installed catalog gates
passed with that deterministic boundary.

[Run 35695556245](https://github.com/supabricks/platform/actions/runs/35695556245)
passed installed identity, data and named upgrade, then timed out in browser
audit pagination after concurrent execution and revocation had passed. Console
#12 serializes audit loads; its harness waits for the displayed page before
advancing. The console change required new archives, so the complete matrix was
rerun in 35700396118. Superseded and failed attempts remain failure evidence;
they do not qualify their archives.

## Deployment boundary

Shared ingress is explicit and requires the operator-reviewed receipt for the
fully verified installed Linux inventory. macOS remains local-owner only.
Chromium is the qualified browser. The OS/Docker owner remains trusted, and
the exact pinned IdP/isolation inputs and host envelope define the tested profile.
Public hosting, publisher signing/notarization, transitive redistribution audit,
physical-machine/reboot/power-loss qualification, other browsers/operating systems
and additional enterprise profiles remain separate work. This engineering
qualification does not advertise those capabilities.
