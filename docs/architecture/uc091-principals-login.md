# UC09.1: principals and login

UC09.1 implements identity/session storage and an authentication-only preview.
IAM00 merged in [platform #66](https://github.com/supabricks/platform/pull/66)
as `d13523b`, after scope #65. Shared governed product ingress remains disabled;
UC09.2 supplies authorization, and UC09.3–.8 integrate data access, isolation,
revocation operations, the full console and release qualification.

## Identity and migration

Schema 16 preserves the existing PK03 realm and local-owner UUIDs. The new
identity registry includes that owner without changing deployment, binding,
branch, catalog or credential ownership. Existing data roots require the normal
stopped, backed-up upgrade; ordinary startup cannot migrate them. The old
`principals` table remains the local deployment contract. UC09.2 must extend
those references to the identity registry when integrating governed admission.

User UUIDs are keyed by exact `(issuer, subject)`, never email or display name.
Service principals and platform-managed groups have independent UUIDs. Group
membership grants no project or data rights in this slice. Disabled users retain
their mappings and cannot authenticate; re-enabling does not restore revoked
sessions. Recreated IdP subjects get new IDs even when their email matches.

The operator explicitly configures a provider and selects the bootstrap subject.
No browser login becomes an administrator automatically. Bootstrap records the
chosen identity for subsequent authorization; the preview exposes no remote
administration endpoints. All administration uses the private operator socket.

## Protocol and session boundary

`identity_auth` uses its own API version (1) and a closed command set. It has no
project, SQL, notebook, catalog, administration or `act_as` operations. The local
console launch-ticket protocol continues to serve only the trusted local owner.
Identity preview sessions cannot be exchanged for those launch tickets.

The maintained `openidconnect` crate implements authorization-code/PKCE and ID
token verification. The adapter binds state, nonce, verifier, callback URL,
client channel and a separate browser/CLI binding secret. Login attempts expire
after five minutes and are consumed before code exchange, including failed
exchanges. Signature, algorithm, issuer, audience, nonce, expiry, optional access
token hash and user-info subject are verified. Discovery/JWKS is fresh on each
exchange, supporting provider signing-key rotation.

Providers and introspection require HTTPS with normal certificate validation;
operator-supplied PEM roots support private on-prem CAs. Redirects must match an
explicit allowlist. Backchannel endpoints stay on the configured issuer's origin,
redirect following is disabled, and response sizes/connect/read times are bounded.
The provider's access token stays in the private control database. Each identity
check requires fresh active introspection with matching issuer, subject, audience
and unexpired token. There is no positive cache or local-owner fallback.

Sessions use random 256-bit opaque credentials; only SHA-256 hashes are stored
for platform credentials and CSRF tokens. Provider tokens/configuration remain
in the existing mode-0600 SQLite state under the mode-0700 data root. User sessions
last no longer than the provider token and at most one hour. No refresh token is
retained. Local logout succeeds during IdP outage. Disable/revoke and session
rotation invalidate credentials; provider reconfiguration invalidates that
provider's pending logins and sessions. Rotation also fences exchanges in flight.

Provider work runs in at most four workers, outside daemon lifecycle processing.
The writer consumes login attempts and commits sessions/audits. On completion it
rechecks provider revision, session epoch, expiry and disabled/revoked state.
Provider I/O never gets the SQLite writer. An audit-write failure rolls back the
identity mutation or credential issuance.

Every authenticated context contains `api_version`, realm, actor, effective
principal, channel, scopes and expiry. Actor and effective principal are equal
in this slice; clients cannot supply either. Service credentials are explicitly
issued by the operator, expire within an hour, and currently accept only the
`identity:self` scope. Future scopes require implemented authorization paths.

## Local authentication preview

Start a source-development daemon using a fresh private data directory:

```sh
cargo run --locked -p supabricks-local --bin supabricks -- daemon --data-dir /tmp/identity-preview
```

Administration requests are private JSON files. For example, a provider request
has this shape (replace placeholders and protect the file with mode 0600):

```json
{
  "action": "configure",
  "provider": "onprem",
  "config": {
    "issuer": "https://idp.example/realms/supabricks",
    "client_id": "supabricks",
    "client_secret": "OPERATOR_SUPPLIED_SECRET",
    "introspection_url": "https://idp.example/realms/supabricks/protocol/openid-connect/token/introspect",
    "redirects": ["http://127.0.0.1:39001/auth/v1/callback"],
    "ca_pem": null
  }
}
```

Configure a confidential Keycloak client with standard flow/PKCE and an explicit
audience mapper including that client in access tokens. Register the exact
callback URI; disable password/direct-access grants for the product client.

```sh
supabricks identity admin --request-file /private/provider.json --data-dir /tmp/identity-preview
supabricks identity login --provider onprem --redirect http://127.0.0.1:39001/auth/v1/callback --output /private/session.json --data-dir /tmp/identity-preview
supabricks identity whoami --session-file /private/session.json --data-dir /tmp/identity-preview
supabricks identity mcp --session-file /private/session.json --data-dir /tmp/identity-preview
supabricks identity logout --session-file /private/session.json --data-dir /tmp/identity-preview
```

Credentials are never printed or passed as command arguments. Output files must
be new, private regular files; input credential files reject symlinks, hard links
and permissions exposing them to other users. MCP reauthenticates each request
and exposes only `identity_whoami`; unauthenticated callers get an error.

`identity browser --provider onprem --redirect http://127.0.0.1:39001/auth/v1/callback`
starts an authentication-only page. It binds **only 127.0.0.1**, checks the exact
Host and mutation Origin, uses HttpOnly/SameSite=Lax cookies and requires CSRF on
login/logout. It clears the callback URL before displaying identity. This local
HTTP preview follows the local console's loopback transport model; it is not a
TLS ingress or a supported shared-server endpoint. Production browser ingress
must use Secure cookies on HTTPS when the later governed acceptance gate enables
it. Cookies and token files are separate channels and cannot substitute for one
another. Unknown routes, including product API routes, are refused.

Other operator JSON actions are `status`, `bootstrap` (issuer/subject/label),
`disable` (principal/disabled), `group` (label), `membership`
(group/principal/present), `service` (label), `issue_service`
(principal/scopes/ttl_seconds), `revoke` (principal), `rotate_sessions`, and
`audit` (after sequence). Service issuance requires `--output PRIVATE_JSON`.
The identity status and audit outputs contain no provider/session credentials.

## Validation and remaining integration

Portable tests cover signed TLS OIDC exchanges, equal/changed emails, recreated
subjects, bad issuer/audience/nonce/signature/algorithm/expiry, callback replay,
redirect/binding mismatch, explicit bootstrap, disabled principals, expiry,
revocation, rotation, restart, audit failure, and in-flight configuration races.
Process tests exercise the actual daemon, CLI, MCP and browser adapter, including
CSRF, duplicate/cross-channel credentials, local-owner fallback rejection and
product route denial. Recovery tests cover schema 15-to-16 and all supported
predecessor migrations, interruption boundaries and immutable source backups.

The [Keycloak qualification](../../e2e/native/identity/qualify.py) runs the actual
binary against IAM00's digest-pinned Keycloak 26.7.4 over TLS, with two users
sharing an email. It verifies IdP disable of an unexpired session, outage,
logout during outage, scoped service credentials, rotation, audit and cleanup.
The `identity-login` workflow repeats this and uploads only a sanitized report.
The fixture's public test keys and Keycloak development mode are never installed
or used for production identity.

This is authentication qualification. It does not claim project/data authority,
execution revocation bounds, restored-session invalidation, production ingress,
full console login integration or exact installed-release governance. Those
remain in the [UC09 plan](../plans/uc09-governed-implementation.md).

Implementation references: [openidconnect](https://docs.rs/openidconnect/4.0.1/openidconnect/),
[OIDC identity stability](https://openid.net/specs/openid-connect-core-1_0.html#ClaimStability),
and the [IAM00 decisions](iam00-governance-probe.md).
