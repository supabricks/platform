# UC09.4: isolated execution and admitted file closure

UC09.4 connects authenticated execution admission and the private UC broker to a
Linux x86_64 execution adapter. An explicitly configured operator can run an
immutable SQL or notebook revision through `identity control`, MCP's
`project_control`, or the loopback authenticated control adapter. Each execution
gets a separate real Jupyter server/kernel and Sail instance inside gVisor.
Shared server ingress and the complete console workflow remain disabled until
UC09.5–.8 qualify the remaining product boundaries.

## Admission and authority

`admit_execution` remains a durable intent. The new `runtime/start` command
consumes that intent once, for the same authenticated actor, session token hash,
effective principal, source revision and project policy revision. It requires an
explicit execution grant, current project membership and an enabled identity.
Service delegation additionally requires the actor's exact-source `act_as` grant
and the service's own membership and execution grant. There is no local-owner
exception in this execution adapter.

The caller supplies at most eight `(publication UUID, publication revision,
table UUID)` selections. Every selection must match the journal and UC's live
ordinary-principal response. The broker still checks identity/object incarnation
and the complete managed grant matrix. No UC token, signing key, administrative
credential, PG connection, host socket or control session enters the sandbox.
Even executions selecting no data authenticate at UC and fail on provider drift
or outage. Project membership never supplies data authority.

The worker verifies the operator's runtime inventory and stages immutable source
and data. It then rechecks live UC authority. The sole writer fences the session,
local policy and catalog revision again, rejects work older than 25 seconds, and
records launch intent plus the runtime
configuration digest before starting the launcher. A failed audit write denies
launch or renewal. Replaying an admission never starts another sandbox. Terminal
results require fresh authentication and the same current project/UC authority;
a result receipt cannot restore revoked access.

Schema 19 adds `isolated_executions`, joining actor/effective identity, source and
policy via the admission ID, and recording dataset revisions, runtime identity,
state and bounded result. Audit entries distinguish prepare, launch, renewal and
finish. Pending/running rows become failed on daemon recovery; no process is
reattached or automatically renewed. Source edits never change a running revision.

## Runtime boundary

The selected launcher is the IAM00 topology integrated into platform code:
a trusted disposable Docker container per execution runs the complete pinned
gVisor distribution. Docker with cgroup v2 supplies the enclosing cgroup and no external network.
The trusted supervisor verifies the actual memory, swap, CPU and task limits before
starting runsc. Nested `--ignore-cgroups` applies only to Docker's read-only cgroup
interface; it does not remove the outer limit. This requires the local rootful
Docker socket at `/var/run/docker.sock`; daemon access to it is an operator
privilege. The socket is never mounted into either container.

The outer container is privileged so gVisor can establish namespaces and mounts.
Only the embedded supervisor runs there. User source, native extensions, ingestion
parsers and executable package preparation execute **inside gVisor**, as UID/GID
1000 with empty capabilities, no-new-privileges, a read-only root/product/admission,
private process/network namespaces and an explicit minimal environment. Runtime
commands clear inherited environment and close unrelated descriptors. No user
request can select a binary, runtime flag, host mount or environment variable.
No host-process fallback exists. A missing Docker/gVisor capability, changed
runtime checksum, invalid cgroup or failed sandbox creation terminates the run.

| Per execution | Bound |
| --- | --- |
| CPU | 2 CPUs |
| RAM / swap | 2 GiB / zero |
| Outer tasks / guest processes | 512 / 256 |
| Guest open descriptors | 256 |
| Scratch, including output/cache/build/spill | 512 MiB |
| `/tmp` / `/dev/shm` | 64 MiB each, charged to RAM |
| Trusted launcher workspace | 768 MiB tmpfs, charged to the same RAM limit |
| Sail memory / spill | 256 MiB each |
| Admitted file copies | 256 MiB and 4,096 files total |
| Returned workload output | 32 KiB before JSON encoding |
| Concurrent executions | 2 |
| Persisted execution records | 128; further admission refuses until operator maintenance |

The proposed dedicated 4-core/16-GiB shared host from IAM00 remains the initial
qualification target. Quota/OOM/output-limit failures return a bounded failure;
the runtime does not retry in a host environment or reuse another user's cache.
The notebook driver executes the admitted cells in order. SQL executes through
the same private Spark session and returns at most 200 rows within the output cap.
Interactive browser channels, arbitrary Spark clients and artifact activation are
still gated. Notebook output is untrusted data, never a control-plane command.

## Files and Delta paths

A workload sees its source at `/admission/source.json` and selected tables at
`/admission/data/<table UUID>`. It receives copied verified files, not a producer's
whole generation directory. Each source path is walked with `openat`,
`O_NOFOLLOW`, regular-file and exact-length checks; the copy's SHA-256 must equal
the immutable manifest. An upstream mutation cannot change an already copied
lease. File counts, total bytes, duplicate names and relative path components are
bounded. Source paths and credentials are omitted from public catalog responses.

The supported closure is a frozen version-zero, unpartitioned Delta table with
reader protocol 1. The one JSON log must contain exactly one protocol and
metadata action and only admitted Parquet additions. Absolute paths, traversal,
encoded paths, URI schemes, deletion vectors, reader features, nonempty table
configuration, extra files, subsequent logs and other action types are rejected.
This deliberately rejects unsupported storage features instead of broadening the
mount. Both native delta-rs/Arrow and Sail read the qualified closure.

Scratch, caches, compiled packages and notebook intermediates disappear with the
sandbox. The immutable staging directory is removed after exit and stale owned
staging directories are discarded on daemon recovery. Recovery bundles exclude
staging and the operator-specific runtime configuration. Restore requires explicit
runtime setup and current catalog reconciliation. Complete governed recovery and
installed component transitions remain UC09.6/UC09.8 qualification work.

## Renewal and failure

A successful authenticated `runtime/poll` renews a running lease only after fresh
identity and live UC checks. Clients should poll every two to ten seconds. The
trusted supervisor accepts bounded absolute monotonic deadlines, at most 30 seconds
ahead. Buffered or replayed old renewals cannot extend a deadline. The renewal
pipe is outside the workload and never mounted into it. Pipe loss or deadline
expiry kills/deletes the whole sandbox, including Jupyter, Sail, native child
processes and already-open files. The daemon also checks local session, policy,
catalog and cancellation state every tick. A revoked slot stays occupied until
the launcher exits, so cancellation cannot bypass the two-execution limit.

An unavailable identity/provider does not renew or return results. On daemon loss,
the supervisor stops independently. This supplies the execution mechanism for the
60-second revocation target; UC09.6 still qualifies the complete cross-backend
revocation/recovery contract, clock changes, restore and all result channels.
Host root, the kernel, Docker daemon and trusted launcher are in the trusted
computing base. Bytes admitted to a workload can be read and transformed by its
code until termination; this is not a data-loss-prevention system.

## Configure and exercise

This is explicit source qualification, not an automatic installer migration.
Use the [IAM00 preparation procedure](../../e2e/native/iam/README.md) to verify the
pinned alpha.34 runtime payload, full gVisor distribution and Ubuntu image. The
platform execution adapter and embedded supervisor come from the candidate
platform build. Paths containing spaces are covered.

```sh
python e2e/native/execution/prepare.py \
  --inputs '/absolute/uc094 inputs' \
  --output /absolute/private-cell/execution-runtime.json
```

The output must be new, privately owned and mode 0600. It binds the release
inventory, full gVisor file inventory and exported rootfs hashes. The compiled
[component pin](../../components/execution-runtime.lock.json) accepts only the
reviewed product and gVisor inventories. Every launch
verifies these inputs. Runtime settings are operator-owned; authenticated users
cannot configure them. Linux x86_64 is the only enabled target.

After ordinary login, project role/execution grant, immutable source admission,
UC principal mapping and reviewed catalog reconciliation, send these command
bodies through `identity control --session-file … --request-file …`:

```json
{"action":"runtime","deployment":"<deployment UUID>","command":{"action":"start","id":"<admission UUID>","datasets":[{"publication":"<publication UUID>","publication_revision":1,"table":"<table UUID>"}]}}
```

```json
{"action":"runtime","deployment":"<deployment UUID>","command":{"action":"poll","id":"<admission UUID>"}}
```

Use the existing authorized `stop_execution` action to cancel. Launching another
actor's admission and reading/renewing another actor's output are denied, even
when project membership permits reading the source.

## Qualification

The catalog-probe workflow runs the real Linux experiment using the pinned UC
server, runtime inputs and Docker/gVisor. Portable tests cover checksum/path/Delta
escape rejection, audit failure before effects, stale policies, disabled sessions,
provider failure and restart closure. The real experiment covers two concurrent
users, denied UC data admission, actual Delta/Spark/Arrow notebooks, native package
preparation and CSV parsing, filesystem/network/socket/environment denial,
read-only source/cache isolation, process/descriptor/scratch exhaustion, and
supervisor cleanup after a running query has opened an admitted file. The OIDC
fixture additionally launches through the real CLI and daemon and disables the
Keycloak user during a running notebook.

```sh
SUPABRICKS_UC093_RUNTIME=/absolute/pinned-uc \
SUPABRICKS_UC094_CONFIG=/absolute/execution-runtime.json \
  cargo test --release --locked -p supabricks-local --lib isolated_execution_real_uc -- --ignored
python e2e/native/identity/qualify.py --binary target/release/supabricks \
  --uc-runtime /absolute/pinned-uc --execution-config /absolute/execution-runtime.json \
  --output /tmp/uc094-identity.json
```

Use release builds for full inventory verification and resource measurements.
The fixture proves the platform admission/broker/runtime composition, not a newly
assembled installed release. No Sail or UC fork change is needed in UC09.4.
UC09.5 next integrates governed PG, ingress data and package/output activation;
those existing broad local-owner entry points remain inaccessible to authenticated
users. The exact installed-release upgrade and shared profile gate remain closed.
