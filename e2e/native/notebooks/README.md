# N01 notebook component probe

This is an isolated engineering fixture, not a console feature or an installer
upgrade. It uses an unchanged, qualified alpha.8 Supabricks release plus a
separate packaged Jupyter dependency tree, a temporary project/data root and a
small authenticated test bridge. It does not register kernels in the user's
Jupyter installation or alter the running Supabricks demo.

The [N01 decision record](../../../docs/architecture/n01-notebook-qualification.md)
records component selection, measurements and required production adapters.
The [notebook plan](../../../docs/plans/notebook-implementation.md) defines N02-N05.

## Reproduce

Build on Ubuntu 24.04 x86_64 or macOS 15 arm64. Builder tools: Node 22, Python
3.12, uv 0.11.21 and `patchelf` on Linux. Datalayer's candidate requires Node 22
through Primer; the selected upstream adapter also builds with the console's
Node 20.20 environment. Node/Playwright are test machinery, not notebook runtime
requirements on the installed host.

Download `release-linux-x86_64` or `release-macos-arm64` from the exact I02
[native-release run](https://github.com/supabricks/platform/actions/runs/34301230539).
Place its `.tar.gz` and `.sha256` files under `build/notebook-baseline-archives`.
Then, from the repository root:

```sh
python3 e2e/native/notebooks/candidate.py --report build/notebook-reports/datalayer.json
npm ci --prefix e2e/native/notebooks/frontend --no-audit --no-fund
npm run build --prefix e2e/native/notebooks/frontend
npm exec --prefix e2e/native/notebooks/frontend -- playwright install --with-deps chromium
uv export --project e2e/native/notebooks/python --locked --format requirements-txt \
  --no-emit-project --output-file e2e/native/notebooks/python/requirements.lock
python3 e2e/native/notebooks/prepare.py --directory build/notebook-baseline-archives \
  --target linux-x86_64 --output build/notebook-package
"build/notebook-package/relocated probe/python/runtime/bin/python3.12" \
  "build/notebook-package/relocated probe/python/notebooks/run.py" \
  --release build/notebook-package/baseline/supabricks \
  --probe "build/notebook-package/relocated probe" \
  --node "$(command -v node)" --harness e2e/native/notebooks/frontend/qualify.mjs \
  --report build/notebook-reports/notebooks.json
```

Use `--target macos-arm64` for the Mac artifact. Use a fresh output directory
for a new build. `prepare.py` verifies the baseline archive and its installed
inventory, builds the probe, archives it, removes the build tree and unpacks it
at a different path containing a space. `run.py` verifies the relocated probe
inventory before execution. The ordinary shell command above does not isolate
host networking; the [CI workflow](../../../.github/workflows/notebook-probe.yml)
uses a loopback-only Linux namespace and macOS Seatbelt for the actual offline
claim. Browser requests are additionally checked against the exact local origin.

The baseline analytical package versions must all remain unchanged in the
candidate Python lock. Only additional, hash-locked wheels install on the
builder; no pip/uv/npm command runs during qualification. Loader checks reuse
already relocated baseline binaries and adjust only new native wheel objects.
Reports include the source commit, predecessor release identity, file inventory,
package versions and notices, component/archive sizes, actual browser/OS context,
API paths/protocol, lifecycle events and resource measurements.

## Fixture boundaries

- `candidates/datalayer/` retains the exact attempted component and lock. Its
  unchanged Vite build fails on the notebook entry's `service-worker?text` import.
  `candidate.py` records that rejection; a changed result requires a new decision.
- `frontend/` embeds the upstream JupyterLab notebook widget in a small React
  adapter. It uses upstream editor/model/rendering/execution APIs, not a custom
  notebook protocol. Static assets are lazy-loaded and all outputs use untrusted
  rendering, even if generated locally. No JavaScript or SVG output renderer.
- `bridge.py` proves cookie/CSRF/Origin/Host checks, server-side upstream tokens,
  single-use WebSocket tickets and the v1 binary kernel protocol. It is an
  isolated Python test server, not production Rust console routing.
- `kernel.py` proves pre-launch A03 admission, compensation after an injected
  failure, and shutdown releasing its analytical session. The harness owns
  Jupyter; Jupyter owns Python; A03 owns Sail. Production daemon process records,
  crash fencing, browser-session ownership and admission races remain N02.
- `run.py` creates synthetic database/notebook files and runs the browser checks.
  Saved documents are validated with nbformat. Private runtime files, launch
  secrets and logs remain under a mode-0700 temporary root; successful runs
  remove it. Failure workspaces are retained locally for diagnosis and must not
  be uploaded wholesale. CI uploads only the designated reports and screenshot.
- Upstream contents saves have no expected-hash conflict contract. The probe
  deliberately observes a stale save overwriting an external edit of a synthetic
  file. N03 must add the architecture's restricted, conflict-aware contents
  adapter before enabling project editing in the product.
- The probe inspects output streaming and configures a protocol frame cap; it
  does not implement the product's aggregate output, cell queue or RSS watchdog
  policy. Python interrupt and Spark cancellation are separate observations.
  Closing the A03 session is the qualified engine termination fallback.

The standalone probe archive duplicates the baseline Python environment to keep
alpha.8 immutable. Its compressed component delta compares the same baseline
Python/libraries/notices with the candidate plus frontend/adapters using gzip
level 1. It estimates incremental packaging cost; it is not the final installer
size, which N02-N05 must measure after production integration.
