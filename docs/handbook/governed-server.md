# Governed Linux server: install, demonstrate and recover

The candidate supports an explicit TLS console and private native services on a
dedicated Linux x86_64 server. Use the exact archive whose complete R04 report
passed, including the inherited Linux/macOS local-owner gates and the Linux
governed gate. The collector emits a private `r04-evidence.governed.json` receipt
bound to that Linux installation. A source-build report or receipt from another
archive cannot enable a network-facing listener. macOS remains local-owner only.

## Server and runtime preparation

Use a dedicated OS account and private data directory (mode 0700). That account
controls the Docker socket and is part of the trusted control plane; do not give
interactive server logins to governed users. Docker and the pinned Ubuntu/gVisor
inputs are operator prerequisites. Untrusted notebook code runs inside gVisor,
with no Docker/control socket, host kernel fallback or external network.

Install the archive through the reviewed installer, then run
`supabricks installation verify`. Keep the exact archive, checksum, complete R04
report and receipt with the deployment record. The receipt is an operator-reviewed
qualification record, not an authentication credential or publisher signature.
Copy it into the private server directory with mode 0600 after reviewing the
complete report and its matching archive identity.

Stage the gVisor distribution and rootfs Docker image named in the installed
`share/governed/execution-runtime.lock.json`. Their reviewed hashes and image
identity are checked; no floating tag or automatic image pull is used at runtime.
From the installed release directory, prepare the private runtime:

```sh
umask 077
mkdir -p /private/server/data
python/analytics/python share/governed/prepare-runtime.py \
  --gvisor-archive /private/staging/gvisor.tar.bz2 \
  --output /private/server/runtime-inputs \
  --config /private/server/data/execution-runtime.json
```

The configuration binds execution to this installation's complete inventory.
The qualification target is a dedicated 4-CPU/16-GiB Linux server.
The initial envelope is two concurrent executions, each with 2 CPUs, 2 GiB RAM,
no swap, 512 tasks and 512 MiB scratch, plus bounded `/tmp` and `/dev/shm`.
Reserve additional capacity for PostgreSQL/storage, UC, the IdP and OS. Admission
rejects additional executions; failure never launches a host kernel. Quotas are
resource limits, not a guarantee that an arbitrary notebook will fit.

## Identity and TLS setup

Configure a TLS OIDC provider and explicitly bootstrap the realm administrator
using `supabricks identity admin --request-file /private/request.json
--data-dir /private/server/data`. Use mode-0600 request files with the following
shapes, replacing the issuer, audience, subject and secret:

```json
{"action":"configure","provider":"corporate","config":{"issuer":"https://identity.internal.example/realms/company","client_id":"platform","client_secret":"REPLACE_WITH_PRIVATE_CLIENT_SECRET","introspection_url":"https://identity.internal.example/realms/company/protocol/openid-connect/token/introspect","redirects":["https://analytics.internal.example:8443/auth/v1/callback"],"ca_pem":null}}
```

```json
{"action":"bootstrap","issuer":"https://identity.internal.example/realms/company","subject":"REPLACE_WITH_ADMINISTRATOR_SUBJECT","label":"administrator"}
```
Match the issuer, client audience and redirect exactly. Email labels do not choose
identity or permissions. Keep the IdP reachable on the explicitly configured
internal network; introspection/provider failure closes governed admission.

Provide an operator-owned TLS certificate and private key (mode 0600). Place this
configuration in a private file; replace the example origin, addresses and paths:

```json
{
  "listen": "0.0.0.0:8443",
  "origin": "https://analytics.internal.example:8443",
  "certificate": "/private/server/tls/cert.pem",
  "private_key": "/private/server/tls/key.pem",
  "qualification": "/private/server/r04-evidence.governed.json"
}
```

Start the native cell with `supabricks up --data-dir /private/server/data`, then
serve the console using the same data directory:

```sh
supabricks console --governed --provider corporate \
  --redirect https://analytics.internal.example:8443/auth/v1/callback \
  --ingress /private/server/ingress.json --data-dir /private/server/data
```

Expose only this TLS listener. PostgreSQL, UC, Sail, the operator socket and legacy
console APIs remain private. The listener checks the configured Host and Origin;
proxy-supplied identity/forwarding headers do not grant authority. Cookies are
Secure, HttpOnly and SameSite. TLS connections, handshake time, command input and
connection lifetime are bounded. A candidate may use loopback TLS without a
receipt solely for qualification; this does not enable a shared listener.

## Installed two-user demonstration

1. Sign in as the bootstrapped administrator and create a project. Grant Alice and
   Bob their project roles and separate branch/data/execution permissions in
   Access. A project administrator role alone does not imply data or execution.
2. Import a bounded `.sbdata` package in Data, prepare a snapshot, review the exact
   revisions and explicitly publish. Enroll catalog identities, review sharing
   for exact publication/table IDs and apply the reviewed plan.
3. Open an independent Bob browser session. Confirm that unshared datasets remain
   absent. After sharing, select readable datasets, save SQL or notebook source
   and run that exact revision. Stored notebook outputs are stripped.
4. Create a service principal, grant it project/data/execution access, and grant
   Bob act-as for the exact saved source revision. Run as that service. Editing
   source requires a new explicit approval.
5. Run Alice and Bob concurrently. Revoke Bob's act-as permission while his
   notebook is running; his results disappear and his entire sandbox stops within
   60 seconds. Alice's independently authorized execution in a separate project remains available.
   A project policy change conservatively fences all work admitted under that
   project's old revision; keep the peer's notebook browser page open for renewal.
6. Review the correlated audit event, then revoke Bob's session. Browser state is
   cleared. Audit export contains identities, revisions and outcomes, not SQL,
   rows, credentials or notebook bodies.

The governed preview accepts `.sbdata` and `.ipynb` uploads up to 22 KB. Foreign
`.sbproj` activation, environment hooks, host kernels, shared Spark/UC/PG clients,
cross-host storage and arbitrary filesystem transfers remain unsupported. The
local-owner console retains its existing offline workflows; it trusts its OS
owner and does not supply multi-user isolation.

## Update, audit and recovery

Drain the TLS console and executions before an update or stopped backup. Stage
the new verified archive, retain the source release, and use the existing
`supabricks installation upgrade --prefix … --previous … --backup …` workflow.
The named alpha.34 UC transition changes only the reviewed server JAR while
requiring identical JRE/H2/dependency/configuration files and unchanged native
engine/analytical compatibility. The source binary checkpoints H2 and SQLite;
the candidate verifies the backup under the data-root lock before migration.
Metadata and publication IDs survive and UC credentials rotate before startup.
Unknown backend/dependency transitions remain rejected.

An interrupted update keeps its durable journal and original backup. Retry the
same update, or restore that backup with its source release into a new private
root. Do not delete guards, rewrite backend contracts or edit a backup to force
compatibility. Re-run installation verification and use a receipt for the new
archive; an old receipt cannot authorize the updated listener.

Governed restore closes admission, revokes old sessions/service credentials and
rotates branch and UC credentials. Use private `identity admin --request-file` requests with `action: restore_status`
and then `action: restore_reconcile` for the recorded `realm_id` and `restore_id` before
regranting access. Historical grants do not reopen automatically. Keep all
execution/TLS/provider configuration and private keys in the recovery plan;
rebind external references explicitly after moving a root.

Export audit pages through the operator interface, archive them privately, then
acknowledge the exact exported page hash and cursor. The browser cannot
acknowledge or prune audit. A full audit closes admission across restart while
remaining exportable. Failed audit writes roll back their associated mutation.
Provider outage, daemon loss and lease expiry stop renewals; the independent
watchdog terminates running work. Diagnose through private operator logs, never
by broadening grants or falling back to local-owner execution.
