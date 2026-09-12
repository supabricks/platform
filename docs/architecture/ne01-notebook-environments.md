# NE01: separate notebook kernel environments

*Qualified for NE02 integration · 2026-09-10 · Production behavior remains unchanged*

## Decision and scope

Use bundled uv 0.11.21 and the existing bundled CPython 3.12.13 to prepare a
project kernel venv. Keep Jupyter Server, Sail, exports and ingestion on service
Python. The kernel contract is the platform-specific transitive closure of
`ipykernel==7.3.0` and `pyspark-client==4.2.0` in the existing notebook lock:
41 distributions on Linux x86_64 and 42 on macOS arm64 (the extra is appnope).
The closure includes Spark Connect's Arrow, Pandas, NumPy, gRPC and protobuf
dependencies. It excludes Jupyter Server, pysail, deltalake, psycopg, pip and the
Spark wheel's build tools.

NE01 supplies an isolated qualification harness and a packaging proposal.
NE02 must implement durable preparation and ownership; NE03 must bind actual
product kernels to those environments. The installed product still uses its
shared packaged environment. There is no package-install command or console
button in this change.

## Evidence and reproducibility

The [probe](../../e2e/native/notebook-environments/README.md) copies the qualified
PR34 native archive from release workflow run `34486403948`. It verifies the
archive hash and full installation inventory, changes only the copied analytical
Python dispatcher, and re-seals a derivative manifest that records the baseline
identity. Its private projects explicitly choose a venv through a fixture file.
That dispatcher is test instrumentation and must never be installed as product
environment-selection behavior.

The production Rust daemon, A03 admission, `supabricks child` gate, private
Jupyter Server, bounded Session and Spark bootstrap are unchanged. The harness
uses the real console session/CSRF API and standard Jupyter websocket messages.
It verifies durable kernel process registration, executable paths, distinct
prefixes, project-local import roots, a real two-row Sail query and `19.75` sum,
interrupt, restart, unchanged epoch, output-limit termination and kernel cleanup.
Both custom projects import xxhash's native extension and different humanize
versions. A separate base-only project boots and queries without either extra.

The workflow runs unprivileged in a loopback-only Linux network namespace or
under macOS Seatbelt with external networking and Homebrew access denied. uv
gets an empty private HOME/cache, an empty tool PATH, explicit bundled Python,
and automatic Python downloads disabled. Registry/build access happens only
during assembly. A controlled loopback index stalls one uv operation to prove
cancellation and retry; this is the only runtime resolver network request.

Both targets passed [run 34497802778](https://github.com/supabricks/platform/actions/runs/34497802778)
at implementation commit `b94836f3ce30b265a827ff5136638d84a522668d`.
The [Linux evidence](ne01-evidence/linux-x86_64.json) and
[macOS evidence](ne01-evidence/macos-arm64.json) retain the complete public reports
and provenance in the repository. Built payloads are retained as workflow
artifacts [Linux](https://github.com/supabricks/platform/actions/runs/34497802778/artifacts/10160681982)
and [macOS](https://github.com/supabricks/platform/actions/runs/34497802778/artifacts/10160734202),
subject to GitHub artifact retention; rebuild from the pins after they expire.

Reports and wheel payloads are produced by
[notebook-environments](../../.github/workflows/notebook-environments.yml).
Each target's `probe.json` records uv source/artifact identities, the service
lock hash, the complete kernel closure, registry wheel URLs/hashes, builder
inputs and the built Spark wheel hash. The payload retains the source-only
Spark input, wheels, uv binary, notices and target requirements. Qualification
reports contain measurements and boolean evidence, not kernel credentials.

## uv invocation and package policy

Create at a unique final path, with no seed packages and no system site-packages:

```text
uv --no-config --offline --no-python-downloads venv --python <bundled-python> <generation>
uv --no-config --offline --no-python-downloads pip sync \
  --python <generation>/bin/python --no-index --find-links <wheelhouse> \
  --only-binary :all: --require-hashes --link-mode copy <target-requirements>
uv --no-config --offline --no-python-downloads pip check --python <generation>/bin/python
```

The protected constraint file must be passed as a percent-encoded `file:` URI.
The pinned uv's `--constraint` handling splits a path containing spaces; the
file URI preserves it and is exercised after relocation into `relocated probe`.
The requirements passed to sync and the wheelhouse path work as ordinary argv
paths. Do not construct a shell command from a manifest.

Keep the entire kernel closure protected at exact versions initially. The probe
rejects a conflicting ipykernel request and an unsupported Windows wheel without
changing the environment. Source-only Spark Connect is built on the builder
using the existing qualified setuptools/wheel/packaging; user machines install
the resulting hash-checked wheel. Other packages must have compatible wheels.
Future changes to a protected pin require requalification, not an ordinary
project package update.

`--link-mode copy` prevents a writable project package from changing the shared
cache or another environment. The probe edits an installed package and hashes
the cache, other project and service before/after. Do not replace copy mode with
hard links. This costs disk per revision and is part of the manager budget.

These options follow uv's [CLI reference](https://docs.astral.sh/uv/reference/cli/)
and [cache model](https://docs.astral.sh/uv/concepts/cache/); the retained native
probe is the compatibility evidence for this pinned version.

## Installation layout and lifetime

Proposed installed layout for NE02 integration:

```text
<release>/python/runtime/                         immutable service Python
<release>/helpers/uv                              pinned native uv (no uvx)
<release>/python/notebooks/kernel-contract.json    target closure and identity
<release>/python/notebooks/wheelhouse/             qualified base wheels
<release>/licenses/uv/                            pinned upstream notices
<data>/notebook-environments/<worktree>/<generation>/  prepared venv
<data>/notebook-environment-cache/                 private disposable uv cache
```

A generation identity includes canonical worktree, manifest/lock digest, target,
interpreter release identity and kernel contract. Publish ready metadata only
after validation. Hold the selected interpreter release and generation while
any kernel leases them. Existing service workers and Sail UDF workers keep their
own service environment; importing a package in a notebook does not provision a
Sail UDF process.

The probe relocates the release before building venvs. It also demonstrates that
moving a venv breaks its absolute entry points and moving its base interpreter
breaks the venv link. Preserve project declarations and rebuild at the new final
path after restore, move or incompatible release change. Do not rename a built
venv as an atomic publish mechanism. This matches Python's documented
[venv portability constraints](https://docs.python.org/3/library/venv.html).

## NE02 budgets and remaining gates

The two-target measurements below include builder-produced Spark wheels and
hash verification. MB uses decimal bytes; filesystem allocation includes block
rounding. Kernel/server RSS is the maximum observed across the three project
queries. Preparation RSS is the OS high-water mark across completed preparation
and test child processes, not a simultaneous-process sum.

| Measurement | Linux x86_64 | macOS arm64 |
| --- | ---: | ---: |
| Kernel distributions | 41 | 42 |
| Wheelhouse, including fixtures | 110.5 MB | 88.4 MB |
| Native uv executable | 60.8 MB | 47.7 MB |
| Retained compressed payload artifact, including source input/notices | 133.9 MB | 108.5 MB |
| Base venv logical / allocated size | 365.1 / 398.1 MB | 308.9 / 342.1 MB |
| Allocated venv / compressed wheelhouse ratio | 3.60× | 3.87× |
| Cache allocated after all fixtures | 399.7 MB | 342.8 MB |
| Cold base preparation | 3.42 s | 7.59 s |
| Warm base preparation | 1.88 s | 3.00 s |
| Preparation/test child peak RSS | 128.4 MB | 74.2 MB |
| Observed kernel / Jupyter Server RSS | 180.9 / 78.7 MB | 183.1 / 91.5 MB |
| Cancellation and clean retry | 1.54 s | 2.21 s |

Adopt these initial **NE02 base/fixture preparation limits**:

- One global preparation at a time, with bounded queuing and cancellation. Build
  before acquiring an analytical session slot.
- A 60-second preparation deadline and 512 MiB total owned-process RSS limit.
  Require cancellation to reap the operation within five seconds, escalating
  if necessary. The probe's cancellation timings include verification and retry.
- Require at least 2 GiB free at admission and keep checking during preparation;
  stop before consuming the final 512 MiB reserve. Bound a generation at 1 GiB
  allocated size. This allows the measured base, a private unpacked cache and
  preparation headroom; it does not reserve space for arbitrary later packages.
- Keep a 2 GiB soft cache budget, pruning only when no preparation owns the cache.
  Leased generations and interpreter releases cannot be evicted to meet it.
  Collect unleased generations through NE02's durable ownership rules.

These limits have headroom over both retained runs. Measurements describe the
qualified base/fixtures on hosted runners, not a latency promise for arbitrary
packages or hardware. NE04 must explicitly revise package-size/time policy when
opening user package resolution. Existing server/kernel/session limits remain
unchanged; Sail RSS and the already installed service are outside preparation's
measurements.

Cancellation here covers an in-flight resolver and a clean retry. NE02 still
owes durable operation state, partial-directory cleanup, process ownership,
crash recovery, ready publication, leases and bounded cache collection. NE03
still owes product selection/provenance and environment-adoption semantics.
NE06 qualifies the final integrated installation and upgrade/restore paths.
This remains local-user Python execution, not a container or security sandbox.
