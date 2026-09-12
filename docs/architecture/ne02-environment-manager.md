# NE02: durable notebook environment preparation

NE02 adds a daemon-owned environment manager and offline native components.
Notebook kernels still use the shared service interpreter until NE03 binds them
to these generations. Dependency editing remains disabled until NE04.

## Try a prepared environment

With an NE02 native installation, run these commands from a Supabricks project:

```sh
supabricks up
supabricks env init --wait
supabricks env prepare --wait
supabricks env status
```

Initialization atomically creates `notebooks/environment/pyproject.toml` and
`uv.lock`. Commit both files. Existing declarations are never overwritten.
`--key REQUEST` makes an initialization or preparation retry reconnect to the
same durable operation. A failed operation requires a new key to retry work.
Without `--wait`, use `env operation ID` and `env cancel ID`. `env gc` collects
unleased, inactive generations belonging to the current worktree.

The default `base` template contains the qualified ipykernel/Spark Connect
client closure. `env init --template fixture-a` and `fixture-b` initialize separate
test projects with the two qualified humanize versions and native xxhash wheel.
Arbitrary edited locks are rejected. These fixtures exercise the ownership
protocol; they are not a general package installer.

## Ownership and publication

The catalog records expected declaration hashes, canonical project/worktree
identity, component contract, request key, state and owned process identity.
Only one preparation runs globally, with at most eight pending operations and
one operation per worktree. Requests with conflicting idempotency parameters fail.

Each generation has a UUID final path under the private data root. Its catalog
record precedes directory creation, and its recorded device/inode is checked
before validation, activation, leasing and collection. Preparation runs through
the existing process ownership gate; its domain record is committed before the
gate permits execution. Cancellation and recovery reap that owned process group.

The bundled Python worker verifies the component inventory, invokes pinned uv
offline with an explicit bundled Python 3.12.13 interpreter, installs hash-pinned
wheels with copy semantics, checks dependencies and exact installed versions,
and verifies interpreter prefixes. It inventories the generation and flushes its
files before reporting success. A single synchronous SQLite transaction commits
the generation, operation and active pointer. A worker report alone never makes
an environment ready. Input changes and failed workers preserve the old pointer.

Ready generations support durable leases for NE03. Lease admission and GC state
changes are serialized in SQLite. GC refuses active/leased generations, checks
directory identity and journals deletion before removing files. Substituted
roots are left for inspection. A retry completes a deletion interrupted after
filesystem removal. On startup, owned workers are reaped before interrupted
operations fail; incomplete generations are invalidated. Notebook recovery runs
before stale leases can be released.

## Components and limits

The native archive includes uv 0.11.21, notices, a target-specific wheelhouse,
three consistent project lock templates, the worker and a hashed kernel contract.
Assembly checks each lock with offline `uv export --locked`. Registry wheels
retain their source hashes. The Spark client wheel is reused only if it matches
service build provenance; otherwise the same pinned source/build tools produce
a wheel whose resulting hash is recorded. Runtime preparation uses no host
Python, uv, pip, package index or build toolchain.

Admission requires 2 GiB free disk. The daemon enforces a 60-second preparation
deadline, 512 MiB owned-process-group RSS and a 512 MiB free-space reserve.
Verification rejects generations over 1 GiB allocated or 100,000 inventoried
files. The disposable cache has a 2 GiB soft budget and is cleaned by the sole
preparation worker. File flushes use bounded concurrency to permit journal group
commit. These are initial qualified-template limits, not OS container quotas.

## Upgrade and recovery

The candidate becomes alpha.9, catalog 10. Upgrade from catalog 8 or 9 requires a
verified stopped source backup. The migration and source identity marker commit
together. Engine/storage/analytical compatibility and immutable installation
verification remain required. Ordinary startup never silently migrates catalogs.

Backups retain environment catalog metadata but omit materialized environments,
worker directories and caches. Project declarations need the same Git/backup
protection as other application source. Startup invalidates missing restored
generations; `env prepare` recreates them. Restore an old backup using its matching
retained source release instead of downgrading an active catalog.

## Validation and boundaries

Rust tests cover every partial journal stage, interrupted paired initialization,
stale inputs, disk admission rejection, cancellation, leases versus collection,
substituted paths, interrupted deletion, and catalog-8/9 upgrade with source
restore. The local native lifecycle harness passed ten checks using actual uv
and environments, including concurrent worktrees, cancellation under five seconds,
stale publication, corrupt wheel bytes, daemon SIGKILL recovery and GC substitution.

The release workflow runs this harness on exact Linux x86_64 and macOS arm64
archives with external networking denied, alongside existing notebook, ingestion,
console and upgrade gates. Both target gates passed at `92ac3a5` in
[native-release run 34504307573](https://github.com/supabricks/platform/actions/runs/34504307573).
Retained reports: [Linux](ne02-evidence/linux-x86_64.json) and
[macOS](ne02-evidence/macos-arm64.json). The same run passed both native stopped
upgrade/restore gates and all existing console, ingestion and notebook checks.
The subsequent portable-test correction canonicalizes the macOS temporary
worktree fixture; it does not change runtime or assembly code. The local run did
not isolate networking. Tests
do not constitute a power-loss or hostile-user sandbox guarantee; project code
continues to run as the local user. Real kernel leases and notebook provenance
are the next slice, NE03.
