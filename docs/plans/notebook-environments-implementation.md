# Managed notebook environments implementation plan

*Status: Proposed; NE00 documentation only · Baseline: platform/main@8556027,
after PRs #33 and #34 · Updated: 2026-09-10*

Follow up the [notebook implementation](notebook-implementation.md) with a
conventional local Python workflow: project dependencies live in a virtual
environment, each notebook has its own kernel process, and the Jupyter service
has a separate, platform-controlled environment. Preserve installation through
one command and the existing local browser experience.

This is a dependency-management feature, not a security sandbox. Code continues
to execute on the host with the local user's permissions. “Industry standard”
here describes an established Jupyter pattern, not a certification or a claim
that all local notebook products behave identically.

## 1. Outcome and baseline

The first user milestone is: open a project, start a notebook without installing
Python, add a library through Supabricks, explicitly restart into the prepared
environment, and import that library without changing another project's packages
or the platform's Jupyter/Sail services. Commit the dependency declaration and
lockfile; a second machine can recreate the environment for its supported target.

Today `crates/local/src/notebooks/runtime.rs` chooses the analytical Python
launcher for both the Jupyter server and notebook kernels. That launcher executes
the bundled Python 3.12 runtime with ambient Python settings/user packages
disabled. Notebook kernels have independent processes, but share packaged
dependencies. `install/native/analytics.py` removes pip/ensurepip from the
distribution; uv is currently a builder tool. Installing packages at notebook
runtime is therefore new behavior, not a UI switch that can simply be enabled.

The shared-environment/build-time-only rules in the existing
[architecture](../architecture/local-notebooks.md) and
[N02 contract](../architecture/n02-notebook-runtime.md) remain current behavior
until the corresponding slices land. This plan proposes their replacement for
notebook kernels only. Existing snapshot, process ownership, authentication,
output, persistence and no-replay contracts remain requirements.

## 2. Architecture decisions

| Concern | Proposed behavior |
| --- | --- |
| Service Python | Keep Jupyter Server, analytical export, Sail and ingestion in the verified installation. Project dependencies never enter their import paths. |
| Notebook Python | One managed virtual environment per project/worktree dependency revision; separate kernel processes may use the same revision. Use the bundled interpreter, without system site-packages. |
| Environment management | Bundle a pinned, inventoried uv executable. Supabricks owns its invocation, arguments, cancellation, cache and operation state. No host uv/Python discovery or automatic interpreter downloads. |
| Project files | Default to `notebooks/environment/pyproject.toml` and `uv.lock`, with an environment selection in `supabricks.toml`. This works in non-Python applications without replacing their root `.venv` or dependency files. |
| Existing Python projects | Explicitly adopt a project-contained uv manifest/lock after compatibility validation. Never silently rewrite an existing manifest or take ownership of an existing `.venv`. External/Conda interpreters and editable/VCS dependencies are deferred. |
| Materialized environments | Private platform data under `notebook-environments/<worktree-key>/<generation>/`; keep environments out of Git. Identity includes manifest/lock content, target, interpreter identity and kernel compatibility contract. Canonical worktree binding prevents two checkouts sharing mutable state accidentally. |
| Base packages | Ship an offline wheelhouse for the supported kernel/client dependency closure and the platform bootstrap adapter. Pin the closure required for ipykernel, bounded transport, Spark Connect and supported Arrow/Pandas interchange; derive it by qualification, not by copying the entire server environment. |
| User packages | Resolve additional wheel-based packages against that compatibility contract. Show conflicts; do not silently replace protected dependencies or change Python versions. |
| Running kernels | Keep their current environment and analytical epoch. Preparing a new environment does not modify running processes. Adopting it requires explicit restart, discarding variables without executing saved cells. |
| Offline behavior | Default/base notebooks work on a clean offline installation. Custom dependencies work offline only when their exact target artifacts are available. Opening a file or starting a kernel never contacts a package index. |
| Package changes | An explicit add/remove/lock/sync operation may resolve/download under the selected network policy. Offline mode rejects missing artifacts with an actionable result. Start does not silently run such an operation. |

Use the upstream model of a Jupyter service connecting to kernels in other
environments, documented by [IPython](https://ipython.readthedocs.io/en/stable/install/kernel_install.html).
uv documents project kernels and separate service environments in its
[Jupyter integration guide](https://docs.astral.sh/uv/guides/integration/jupyter/).
These references support the separation; Supabricks' lifecycle and compatibility
policies above are product decisions.

### Reproducibility and activation

Use standard `pyproject.toml`/`uv.lock` semantics. A normal sync verifies that the
lock matches the declaration and installs the locked selection; it does not
silently re-resolve. Dependency edits require an explicit lock/update operation.
uv distinguishes locked validation from frozen operation; select and test the
appropriate invocation against the pinned binary using its
[locking and syncing contract](https://docs.astral.sh/uv/concepts/projects/sync/).

Keep resolution, artifact acquisition, environment construction, verification
and activation distinct. Record an operation journal and expected input revisions.
If files change during preparation, do not activate the obsolete result. Managed
edits to the manifest/lock pair need interruption recovery and conflict handling;
two independent file renames are not a transaction with external editors.

Create each venv directly at its final unique private path, and publish its ready
record/pointer only after verification. Do not build it elsewhere and rename it:
venvs can contain absolute interpreter/script paths and are not generally
portable, as explained in [Python's venv documentation](https://docs.python.org/3/library/venv.html).
Use copies or a qualified copy-on-write strategy for installed artifacts; writes
inside a kernel environment must not modify a shared wheel cache, another
environment, or the service installation through hard links.

Verify the selected interpreter, distribution inventory, protected dependencies
and bootstrap imports before granting a ready environment lease. Treat these as
reproducibility checks, not protection against the same OS user modifying files.
Detect supported forms of external package drift and require a rebuild before
the next start; do not claim continuous tamper prevention during arbitrary Python
execution. Untracked `%pip`/`!pip` mutations are not the managed installation path.
Provide a clear pointer to the supported package action; do not ship pip into the
service environment to make these magics work.

### Lifecycle, limits and recovery

Environment operations are asynchronous, project-authorized, idempotent and
revision-checked. Reuse process supervision for uv/verification children with
recorded identities before launch. Bound concurrency, elapsed time, download
bytes, expanded bytes, disk reserve and retained cache/environment storage.
NE01 measures initial budgets; NE02 makes them explicit enforced policies.
Cancellation and daemon recovery remove only owned incomplete generations.

Prepare an environment before acquiring scarce A03/Sail admission. Pin a ready
environment generation when starting a kernel; a generation cannot be collected
while any kernel uses it. Activation affects future starts. Plain restart retains
the existing environment and epoch; **Use updated environment** replaces only
the environment while retaining the epoch. **Use latest snapshot** changes only
the epoch unless the user also chooses a different environment. Missing saved
environment revisions produce a recovery choice, never silent substitution.

Save a relative environment reference and lock/contract identity in notebook
metadata. Saved cell-output provenance records both environment and epoch;
rerunning one cell does not relabel previous outputs. Do not persist interpreter
paths, index credentials, connection endpoints or tokens in notebooks.

Back up project declarations/locks with application source. Record environment
intent in platform recovery metadata if needed, but rebuild venvs rather than
restoring absolute-path executable trees. Offline restoration of custom packages
also requires their artifacts: offer an explicit verified wheel bundle export/
import, separate from ordinary database backups. Retain the source installation
while environments reference its interpreter. An upgrade validates compatibility
and rebuilds for the new interpreter before adoption; it does not retarget venv
symlinks or restore running kernels. Allocate a catalog migration from the actual
merge baseline if durable records require it, with the backed-up upgrade path.

### Package and execution boundaries

The first supported additional-package path is registry wheels for Linux x86_64
and macOS arm64 using the packaged Python version. Disable implicit source builds
and unmanaged index/config discovery. Fail clearly for unavailable native wheels,
incompatible Python requirements, unsupported sources and protected-package
conflicts. NE01 must qualify exact uv options, configuration precedence, artifact
hashes and native loader behavior rather than assuming defaults.

Use the configured package index for explicit online operations; package-index
credentials stay in private runtime configuration and redacted logs. Multiple
private indexes, remote code builds and additional Python versions are later
features. Offline base startup must be tested without a builder's cache.

Project packages initially apply to the notebook's Python process. Sail Python
UDF workers are separate processes: installing a package in the notebook venv
does not make it available there. Keep the currently qualified Sail worker
environment and clearly document that boundary. Arbitrary project dependencies
inside Sail UDFs require a separate source-controlled worker-environment design
and qualification; do not launch or replace the Sail engine from the user venv.

## 3. Repository ownership

| Repository / location | Responsibility |
| --- | --- |
| `supabricks/platform`: proposed `crates/local/src/environments/` | Resolver/build operations, revision identity, leases, activation, cleanup and status |
| platform: `cli.rs`, `api.rs`, `daemon.rs`, store, supervisor and console routes | CLI/agent contracts, authorized actions, asynchronous process ownership and any migration |
| platform: `crates/local/src/notebooks/`, `python/notebooks/`, `python/analytics/runtime_environment.py` | Choose the leased venv interpreter through the existing child gate; separate kernel-contract validation from the service's exact-inventory check |
| platform: `components/`, `install/native/`, Python locks | uv/interpreter provenance, offline base wheelhouse, kernel contract, licenses, native packaging and upgrade compatibility |
| `supabricks/console`: `src/api.ts`, `src/notebook.tsx`, `src/notebooks/` and product scripts | Environment/package UI, progress/conflicts, explicit adoption, output provenance and real browser scenarios |
| platform: `e2e/native/notebooks/`, native release/recovery workflows | Fault injection and exact-archive qualification across both targets |

Follow the [console ownership contract](../architecture/console-source-split.md):
frontend changes land in `supabricks/console`; platform integrates a reviewed
gitlink and coordinated API capability. Do not resume editing the submodule as
if it were platform-owned source. No Neon/Postgres engine changes are required.
Use the analytical component source/pins present at implementation time; this
plan neither replaces nor marks any Sail source-build effort complete.

## 4. Slices and acceptance

NE identifiers are new and do not rename the historical N00–N06 work. Each slice
lands through a reviewed PR with linked evidence. No runtime change is authorized
by marking this document complete.

### NE00 — Record this follow-up

**Deliverable:** this plan and links from the notebook roadmap/handbook.
**Acceptance:** ownership, offline policy, compatibility, environment/epoch
semantics and the first executable slice are concrete. Validate links and status
claims. No package, running-demo or service changes.

### NE01 — Qualify separate service and kernel environments

**Depends on:** NE00. **Touch:** an isolated native probe, component/Python lock
proposals, and a decision/evidence record. Do not enable user package installs yet.

- Pin and inventory target uv binaries with source identity, checksums and notices.
  Package the base kernel wheels, including builder-produced wheels where the
  existing qualified dependency was distributed as an sdist. Record exact build
  inputs and resulting artifact hashes; never build these on the user's machine.
- Create a real venv using bundled Python and no system site-packages. Launch
  Jupyter from the service environment and its kernel from that venv via the
  existing gate. Derive the smallest supported kernel dependency contract.
- Prove the existing bounded Session/bootstrap works; import an extra pure-Python
  wheel and a native wheel, query real Sail data, and interrupt/restart cleanly.
  Use different compatible package versions in two project environments.
- Exercise clean offline creation, interpreter/venv relocation constraints,
  protected-package conflict, unsupported wheel, cache mutation isolation and uv
  cancellation. Measure package sizes, disk amplification, cold/warm preparation
  and RSS on both targets; set initial manager budgets and installation layout.

**Acceptance:** retained Linux/macOS reports prove distinct interpreter prefixes
and import roots, unchanged service/other-project inventories, real Spark results,
and offline base startup without host Python/uv or a populated cache. A package
import in the already bundled environment is insufficient.

**Decision gate:** accept uv options, compatibility closure and wheelhouse layout
before runtime integration. If any fails, revise the design with measured evidence.

### NE02 — Implement environment operations and durable ownership

**Depends on:** NE01. **Touch:** environment manager, supervisor, typed contracts,
store/recovery as required, native component assembly.

- Implement inspect/initialize/prepare/status/cancel and generation leases. Keep
  user dependency mutation disabled until NE04; prepare the base and predeclared
  fixture locks only. Journal operations and expected manifest/lock revisions.
- Build at unique final paths, verify before ready, atomically activate metadata,
  serialize changes per canonical worktree and enforce measured resource limits.
- Recover failed/cancelled preparations, reconnect idempotent operations after a
  daemon crash and collect only unleased, known generations. Preserve a usable
  previous generation after disk/network/build failures.
- Stage any schema and compatibility change with stopped backups and migration
  fixtures; do not bypass installation identity checks for development convenience.

**Acceptance:** crash injection at each stage, two concurrent prepares, stale
inputs, symlink substitutions, corrupt artifacts, disk-full, cancellation and
GC-versus-lease races cannot activate partial environments or kill unrelated
processes. Project manifests survive conflicts and interrupted paired updates.

### NE03 — Bind kernels and notebook provenance to environments

**Depends on:** NE02. **Touch:** notebook coordinator/gate/bootstrap, status and
capabilities, persistence validation, runtime fault tests.

- Select only a ready, project-owned generation; acquire its lease before kernel
  launch and release on every shutdown, failed bootstrap and crash path. Start
  Jupyter/Sail with their service runtime. Preserve existing A03 admission limits.
- Add environment identity alongside epoch identity to handle status and saved
  provenance. Define handling for older notebooks lacking environment metadata:
  offer the release's offline default on explicit start, and record that binding.
- Implement ordinary restart retaining both identities, explicit environment
  adoption retaining the epoch, and separate latest-snapshot selection. Never
  replay execution to rebuild variables after adopting dependencies.
- Reject missing, incompatible or drifted environments before launching Python.
  Changed declarations/locks mark preparation needed; they do not mutate a live
  kernel or trigger network resolution.

**Acceptance:** two projects import different versions concurrently, same-project
old/new kernels remain on their leased generations, service imports stay fixed,
and mixed environment/epoch output provenance survives save/reopen. Existing
authentication, output limits, cancellation and ownership regressions pass.

**First runtime milestone:** installed offline notebook runs through an isolated
managed kernel environment using the existing console's default start path.

### NE04 — Add reproducible package workflows and CLI/agent actions

**Depends on:** NE03. **Touch:** typed environment commands, resolver/file edits,
artifact cache and offline bundle import/export. Proposed CLI surface:

```text
supabricks env init
supabricks env status
supabricks env add scikit-learn
supabricks env remove scikit-learn
supabricks env lock
supabricks env sync [--offline]
supabricks env export-bundle PATH
supabricks env import-bundle PATH
```

Commands are proposals, not available features. Reuse platform operation IDs,
expected revisions, cancellation and bounded status results; do not add a shell
command execution endpoint. Expose equivalent typed agent actions where the
existing CLI/MCP capability contract supports them.

- Add/remove update declared intent and lock through the managed transaction,
  then prepare a replacement generation. Show resolved package/version changes,
  source/network policy, conflicts, and whether kernels need explicit adoption.
- Support adopting an existing project-contained uv declaration explicitly;
  document the supported subset and reject conflicting Python/contract constraints.
- Verify offline bundle contents, hashes, target and limits; never extract paths
  outside the owned cache. Do not promise that a lockfile contains package bytes.
- Keep unsupported `%pip`/shell changes visibly outside reproducibility guarantees;
  provide a managed action and useful missing-package/conflict guidance.

**Acceptance:** add/import/remove, incompatible versions, network loss, missing
offline artifacts, concurrent external edits, malicious archive paths and replayed
operation IDs behave deterministically. Rebuilding from committed inputs on a
clean target returns the selected versions. A package operation never modifies
the currently executing kernel or acquires an analytical session unnecessarily.

### NE05 — Integrate environment controls in the console

**Depends on:** NE04; API/capability contract can be coordinated earlier.
**Touch:** `supabricks/console`, followed by a platform gitlink/integration PR.

- Show the selected Python/environment, installed/declared packages, preparation
  progress and errors. Base environment preparation follows explicit Start kernel;
  custom packages requiring a download offer a separate Prepare action.
- Add/remove packages through authenticated structured actions, with conflict
  and offline explanations. Support cancelling work without discarding edits.
- Distinguish ready-for-next-start from currently-running environment. Offer an
  explicit restart to adopt changes, explain variable loss, and preserve the
  analytical epoch. Keep snapshot refresh and environment changes independent.
- Show saved-output environment provenance; handle missing locks, moved projects,
  stale revisions and unsupported package magics. Keep infrastructure details
  out of the main notebook flow; diagnostics may expose paths/identities.

**Acceptance:** real product browser tests cover two projects, package add/import,
failed dependency resolution, offline preparation, old/new kernel versions,
explicit adoption, save/reopen and unchanged epoch. Test against an older runtime
capability response and preserve the supported baseline experience.

### NE06 — Qualify installation, upgrades, recovery and documentation

**Depends on:** NE05. **Touch:** release assembly, native/offline/recovery jobs,
runbooks, example projects and compatibility records.

- Qualify exact relocated archives on Linux x86_64 and macOS arm64. Prove base
  preparation and notebook operation offline without global tools or warm caches.
  Use a controlled local package index for reproducible online/failure scenarios.
- Restore project inputs plus an explicit artifact bundle into new paths and
  rebuild venvs. Exercise upgrade while old environments exist, compatibility
  refusal, retained interpreter releases, down/up, crash recovery and leased GC.
- Run the existing Postgres, analytics, console, ingestion, notebook and recovery
  gates; record archive identities, source pins, package hashes, notices, measured
  resource costs and supported wheel/platform matrix.
- Document package actions, offline limits, lockfile review, project adoption,
  drift recovery, local-user execution and the Sail UDF dependency boundary.

**Acceptance:** a non-builder installs, starts an offline base notebook, adds a
supported dependency, explicitly adopts it and recreates the project elsewhere.
All retained fault and native qualification reports refer to the final release
revision. No “industry-standard” completion claim rests on a source-only demo.

## 5. Sequence and deferred scope

`NE00 -> NE01 -> NE02 -> NE03 -> NE04 -> NE05 -> NE06`.
NE01 is the next implementation slice. The first reviewable artifact should be
the two-target separation probe and its packaging/compatibility decision, before
adding a package-install button to the product. Split individual slices into
backend/API and frontend pin PRs where necessary without weakening their gates.

Containers/VM sandboxes, remote kernels, hosted-console connectivity, arbitrary
Python/Conda versions, GPU/system package management, editable/VCS/source builds,
private-index administration and dependency propagation into Sail UDF workers
are separate follow-ups. This plan also does not mark the remaining analytical
SQL, ingestion or broader notebook acceptance work complete.
