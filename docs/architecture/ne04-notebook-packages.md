# NE04: reproducible notebook package workflows

NE04 adds managed registry package operations to the local CLI and MCP. The
bundled uv 0.11.21 resolves dependencies for bundled Python 3.12.13. The daemon
prepares an isolated replacement generation and publishes its declaration pair;
running kernels keep their existing generation and analytical snapshot.

## Use it

Run these commands in an initialized, running Supabricks project:

```sh
supabricks env init --wait
supabricks env add 'scikit-learn' --wait
supabricks env status
supabricks env remove scikit-learn --wait
supabricks env lock --wait
supabricks env sync --offline --wait
```

`add`, `remove`, and `lock` resolve explicitly and prepare a replacement. `sync`
checks the existing lock against declared intent without re-resolving it; omit
`--offline` to allow acquiring missing locked wheels. `prepare` remains an offline
alias for preparing the current declaration. Starting a kernel also prepares
only offline; opening a notebook never downloads packages.

Commit `notebooks/environment/pyproject.toml` and `uv.lock`. The managed directory
must contain only those two files so a transaction cannot move unrelated files.
Use the existing console **Use prepared environment** action to adopt a ready
generation. It discards variables without replaying cells and retains the
analytical snapshot. Full package controls are described in [NE05](ne05-console-environments.md).

`env status` reports protected package versions, declaration hashes, active
identity, preparation status and recent operations. Mutations return durable
operation IDs immediately. `--wait` polls for up to 210 seconds; expiration does
not cancel or resend a mutation. Poll with `env operation ID`, cancel with
`env cancel ID`, and reclaim inactive, unleased generations with `env gc`.
`--key KEY` identifies a request; reusing it with different parameters conflicts.
Typed operations carry expected manifest and lock hashes, so an external edit
cannot silently become the input to an already accepted request.

## Supported declarations and dependencies

The first index policy is the single public index `https://pypi.org/simple` with
wheel downloads restricted to `https://files.pythonhosted.org`. Ambient uv/pip
configuration, credentials, proxies and interpreter selection are not inherited.
There is no private-index credential UI or arbitrary index option in this slice.
`--offline` disables uv networking and artifact downloads.

Accept registry PEP 508 requirements (including compatible extras and markers).
Protect the qualified ipykernel/Spark Connect/Arrow/Pandas dependency closure with
exact constraints. Reject incompatible versions, alternate Python versions,
URLs, VCS/editable/path dependencies, build systems, dependency groups, workspaces
and custom uv source/index settings. The service runtime is never modified.

To adopt an existing declaration, initialize the managed pair first, then run:

```sh
supabricks env adopt path/to/python-project --wait
```

Use `env adopt .` for a declaration at the project root. The path is relative to
the bound worktree and must contain `pyproject.toml` and
`uv.lock`, without symlink components. The supported source is a virtual uv project
with static name, version, Python constraint and dependency list (description is
allowed). Its Python constraint must admit 3.12.13. Adoption copies the intent,
adds the kernel roots/constraints, resolves and prepares privately; source files
and a root `.venv` stay where they are. Both source and destination hashes are
checked before publication.

uv resolves with source builds disabled. PySpark Client is the one release-time
qualified source distribution: resolution uses the already shipped wheel, then
restores its exact qualified upstream lock block. This avoids embedding a local
wheel path in the committed lock. Runtime package operations never rebuild it.

`%pip`, `%uv` and Conda-family package magics direct the user to `env add` instead
of mutating a running generation. Missing-module errors include the same guidance.
Arbitrary notebook code still runs as the local user; this is not a sandbox.
Manual shell mutations are outside the reproducibility guarantee and admission
inventory checks reject a drifted generation. Notebook packages do not change
Sail's separate Python UDF worker environment.

## Offline transport

```sh
supabricks env export-bundle /absolute/path/environment.zip --offline --wait
# On another machine with the same target and environment component:
supabricks env init --wait
supabricks env import-bundle /absolute/path/environment.zip --wait
supabricks env sync --offline --wait
```

Bundles contain the declaration pair, complete selected wheel bytes, target,
component contract identity and SHA-256 inventory. A lock alone contains no wheel
bytes. Import is always offline and prepares a fresh generation. A clean target
can reconstruct the selected versions after import; virtual environments are
never copied between machines. Cross-target or incompatible component bundles
fail and must be re-exported for the intended release/target.

The ZIP transport rejects duplicate/traversal/absolute paths, symlinks, special
files, encryption, unexpected entries, excessive sizes and mismatched hashes.
Wheel expansion is inspected before uv installation. No archive extraction API
writes caller-provided paths. Artifacts are content-addressed in a private cache;
installation always copies bytes. Export uses exclusive staging in the explicitly
named destination directory and publishes without overwriting an existing file.
An interrupted export can leave `.supabricks-bundle-<operation-id>.tmp`; it is
never presented as a completed destination.

## Transactions, recovery and limits

One daemon worker serializes package operations and cache writes. It journals the
request before launch, checks expected inputs again at start, and runs only the
hash-verified bundled worker/interpreter. The worker edits private copies, resolves,
acquires wheels, builds at the final private generation path, checks the package
closure, imports the bootstrap dependencies and fsyncs the inventory. A transient
empty uv workspace bounds discovery to the private project; `--no-config` alone
does not prevent uv from selecting an ancestor workspace. The boundary is removed
from the staged manifest after each resolver/export command.

After validation the daemon journals the resulting hashes, then atomically
exchanges the managed declaration directory with a staged complete pair
(`renameat2(RENAME_EXCHANGE)` on Linux, `renameatx_np(RENAME_SWAP)` on macOS).
The old pair remains at `notebooks/.environment-<operation-id>`. Concurrent edits
prevent activation; an edit through an already-open descriptor is preserved in
that retained revision. No filesystem API can stop an uncooperative editor from
writing its old descriptor, so both revisions are retained for inspection.

SQLite activation is a separate durable transaction after pair publication. A
crash can therefore leave a complete new pair with the previous active generation.
Recovery marks interrupted work failed, invalidates unfinished generations and
keeps that previous active identity. Inspect both retained pairs, keep the intended
one, then run `env sync` with a new request key. Recovery never guesses which edit
to discard or authorizes a generation from a report alone.

Package operations retain the existing queue limit of eight, one writer, 512 MiB
aggregate process RSS, 2 GiB admission free space and 512 MiB free-space reserve.
They have a 180-second deadline (ordinary qualified preparation stays at 60),
55-second subprocess deadlines, two concurrent uv downloads, a 512 MiB wheel
transfer/bundle limit and a 1 GiB expanded-wheel/generation limit. Resolver cache
and artifact cache are each limited to 2 GiB; private operation scratch is bounded
to 3 GiB. Completed/failed operations discard bulk transfer scratch while retaining
the small declarations and private diagnostics. Generation collection respects
leases; preparing packages never consumes analytical admission.

The artifact cache, resolver cache, generations and operation scratch are excluded
from physical backup. Keep declaration pairs with source and use a wheel bundle
when restoration must be offline. Upgrade/restore across component changes and
final product qualification remain NE06. This slice advances the release to
`v0.1.0-alpha.11` and retains catalog 10; new journal fields are optional JSON.

## Agent and validation contracts

MCP exposes `env_inspect`, `env_declaration`, `env_initialize`, `env_manage`,
`env_find`, `env_status`, `env_cancel` and `env_collect`. `env_manage` accepts a
strict typed change (`add`, `remove`, `lock`, `sync`, `adopt`, `export_bundle` or
`import_bundle`), expected hashes, network policy and request key. It has no shell
command field. Status includes resolved versions, changes, source/network policy
and the need for explicit kernel adoption.

Rust tests cover complete pair exchange, interrupted publication, external edits,
scoped idempotency, ownership and the CLI's combined offline/wait flags. Python
unit tests exercise hostile archive paths and artifact source restrictions.
`e2e/native/notebook-environments/packages.py` runs real daemon operations,
imports added wheels, removes packages without changing old generations, exports
and imports bundles after deleting both caches, checks hostile/hash-corrupt
bundles, adopts project declarations and fences external edits. CI runs this
against the exact native Linux and macOS archives, alongside the existing NE02
fault and NE03 kernel suites.
