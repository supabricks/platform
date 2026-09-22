# UC09.7: signed-in console workflows

The governed console is a separate loopback entry point in the existing console
bundle. It uses the UC09 browser OIDC/PKCE session and typed authorization API;
it never acquires a local-owner console ticket. Shared network ingress remains
disabled until UC09.8 qualifies the complete installed profile.

## Launch and administrator setup

An operator configures the OIDC provider and explicitly bootstraps the realm
administrator using the UC09.1 setup workflow. Configure the managed local UC
provider, native PostgreSQL/analytical exporter and qualified Linux isolated
execution runtime before demonstrating data publication and Spark execution.
The redirect must exactly match the configured provider redirect:

```sh
supabricks console --governed --provider corporate \
  --redirect http://127.0.0.1:43127/auth/v1/callback \
  --data-dir /private/platform-state
```

Open the printed `/auth/v1/console` URL. The provider performs sign-in; session
credentials remain in HttpOnly, SameSite cookies. The page displays the actor,
realm, expiry and administrator status and provides sign-out. Forms and commands
require the exact loopback Origin, Host and CSRF binding. A bounded browser worker
pool lets another session revoke access while execution admission is pending.

`supabricks console` retains the local-owner launcher, reconnect behavior and
offline workflows. That mode trusts the host OS owner and does not provide
multi-user isolation.

## Normal browser workflow

1. Sign in as the explicitly bootstrapped realm administrator. Create a project;
   its private worktree, deployment and initial database are allocated by the
   server. The creator receives a project administrator role. Data and execution
   permissions are separate, explicit grants.
2. Use Access to grant project membership and branch read/write/DDL, receive,
   copy-source or share capabilities. Administration provides groups, service
   principals, group membership, session revocation and principal disable/enable.
   Only the realm administrator can grant data, execution or service-use authority.
   Project administrators can manage their own project membership.
3. In Data, import a reviewed `.sbdata` package once the branch is ready. Receive
   and DDL grants are required; existing transactional import and authority checks
   remain authoritative. Prepare a snapshot, review its exact source/binding
   revisions and confirm publication.
4. Enroll identities in the managed catalog, review current grants, then review
   and apply sharing for exact publication revisions and table IDs. The grant
   plan exposes the before/after privileges. Server fences reject changed plans.
   UC discovery returns only metadata allowed for the current identity.
5. SQL supports governed PostgreSQL commands and immutable SQL source admission.
   Notebooks imports `.ipynb`, edits source cells and strips stored outputs and
   runtime metadata before persistence. Select readable datasets and run the
   saved source revision through the isolated Spark runtime. Dataset paths shown
   in the UI refer only to the private admitted copy inside the sandbox.
6. Optional service use binds the actor, effective service principal and exact
   source revision; a service role or execute grant alone does not imply act-as.
   Terminate an execution or revoke its permission/session from another browser.
   Polling rechecks authority and clears unavailable results; session loss removes
   project/source/result state. Audit pages correlate actor, effective identity,
   policy, source, datasets and execution without SQL text, rows or credentials.

## Durable boundaries and limits

Schema 22 adds `governed_project_requests`. Creation keys are actor scoped, bind
names and server-selected directories, and recover filesystem/native-journal
boundaries. A retry does not recreate revoked roles. Native branches belong to a
governed deployment from their first durable intent, including a crash before
branch enrollment finishes. Upgrade accepts schema 21 through the stopped backup
workflow. Governed restores retain UC09.6's closed state and credential rotation.

The browser forwards only typed commands. Provider configuration, bootstrap,
backup/restore, audit acknowledgement, host paths, operator envelopes and legacy
`/api` routes are unavailable to signed-in requests. Administrative policy and
identity actions are attributed to the authenticated actor. Catalog publication
requires Share; browser responses omit storage locations and native diagnostics.

This preview accepts bounded `.sbdata`/notebook uploads up to 22 KB. PostgreSQL
operations retain the supported whole-branch profile and transactional limits.
Foreign `.sbproj` activation, environment hooks, host notebook kernels, shared
network ingress and unqualified transfer modes remain unavailable. Larger
uploads and other interactive ingest formats are not claimed by this profile.

## Qualification

`e2e/native/governed-console/qualify.py` provisions disposable pinned TLS Keycloak,
a real native PG17 cell, managed UC and the configured gVisor runtime. It invokes
`console/scripts/governed-qualify.mjs` with independent Alice/Bob Chromium contexts.
Operator setup is separate from browser project creation, import, publication,
sharing, execution and revocation. No grant files or UC CLI commands perform the
normal demonstration. The report records individual checks and failures.

Run with the source binary, pinned native release and isolated runtime inputs:

```sh
python e2e/native/governed-console/qualify.py \
  --binary /path/to/supabricks --release /path/to/native-release \
  --uc-runtime /path/to/managed-uc --execution-config /private/execution-runtime.json \
  --console /path/to/console --report /private/governed-console-report.json
```

Use an optimized binary for the qualified isolated execution deadlines. The
console's existing `scripts/qualify.mjs` remains the local-owner browser regression
gate. UC09.8 must bind all inherited and governed reports to the exact installed
archive before advertising a shared governed release.

### Source qualification results (2026-09-21)

The [Linux governed browser report](uc097-evidence/linux-governed-browser.json)
passed all 11 scenarios against the real native, managed UC, Keycloak and gVisor
processes. The SQL result was exactly `[{"total":42}]`. A notebook running as a
service principal read the admitted dataset and wrote a readiness marker inside
its sandbox before Alice revoked Bob's source-specific act-as grant. Bob lost
access and the sandbox process stopped within 1,412 ms. The audit event retained
Bob, the effective service, source revision and dataset publication without the
notebook contents. All owned fixture processes were cleaned up successfully.

The [local-owner browser report](uc097-evidence/linux-local-owner-browser.json)
passed 53 checks covering native SQL, ingestion, analytics, packaging, project
creation, restart/reconnect and sign-out. The portable Rust suite passed 286 tests
with three intentionally ignored native tests, including schema-21-to-22 upgrade
and governed closed restore. Release-evidence collector tests also passed.

These are source-build Chromium results on Linux, with loopback page requests
and host networking available. They do not certify an installed archive, shared
TLS ingress, macOS governed execution, Safari, Firefox or offline host networking.
