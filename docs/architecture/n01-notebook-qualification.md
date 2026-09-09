# N01: notebook component and kernel qualification

*Status: Qualified for N02 integration · Date: 2026-09-09*

N01 implements the isolated [notebook probe](../../e2e/native/notebooks/README.md)
from the [implementation plan](../plans/notebook-implementation.md). It uses the
qualified I02 alpha.8 release from native-release run `34301230539`; the shipped
console, runtime formats and analytical package pins are unchanged. Notebooks
are not enabled in the product by this PR.

## Component decision

Select a thin React adapter over upstream JupyterLab's notebook widget for the
first Supabricks notebook. Keep Jupyter Server and ipykernel as the native Python
execution stack. Both targets passed the native qualification below.

| Candidate | Exact input | Observation |
| --- | --- | --- |
| Datalayer React notebook | `@datalayer/jupyter-react` 2.0.14, retained npm lock | Unmodified notebook entry fails under the existing Vite configuration: a JupyterLite `service-worker.js?text` import has no default export |
| Upstream notebook widget | `@jupyterlab/notebook` 4.6.3, `@jupyterlab/services` 7.6.3, retained npm lock | React adapter builds under the existing React 19/Vite versions and executes a real notebook against A03 |
| Jupyter Server | 2.21.0 | Standard contents/session/kernel APIs behind the private test bridge |
| Python kernel | ipykernel 7.3.0 | Full Spark Connect endpoint bootstrap with the existing PySpark client and Sail 0.7.1 |

The Datalayer attempt installs 972 packages including a Node-22-requiring Primer
component. It is retained as a reproducible comparison, not described as
impossible to adapt. The selected adapter avoids that extra component layer and
requires no dependency version override. The initial upstream dependency set
installs 312 packages; exact resolved and optional target packages are in its
lock. Its JS style entry avoids the CSS entry's Webpack-specific `~` imports.
Both approaches still use upstream JupyterLab internals, so upgrades require
rerunning the probe. Upstream licenses and package metadata are preserved in
the fixture rather than relicensed as Supabricks code.

The [Datalayer component](https://jupyter-ui.datalayer.tech/docs/components/notebook/)
is a documented embedding option; its actual npm artifact determines this
compatibility result. The selected adapter uses upstream
[Jupyter Server HTTP APIs](https://jupyter-server.readthedocs.io/en/latest/developers/rest-api.html)
and the [v1 kernel WebSocket protocol](https://jupyter-server.readthedocs.io/en/latest/developers/websocket-protocols.html).
No custom execution protocol or Jupyter/Sail fork is introduced.

The macOS pyzmq 27.2.0 universal2 wheel contains an unused `/tmp/zmq/lib`
LC_RPATH in `zmq/.dylibs/libsodium.26.dylib`. Inspection of both Mach-O slices
shows system dependencies and existing `@loader_path` links to bundled ZeroMQ
libraries. Assembly removes only this known RPATH and applies/verifies an ad-hoc
signature on the changed library; all other external paths remain errors.
This is a packaging relocation adjustment, not publisher signing/notarization.

## Integration findings

| Area | Observed behavior and next implementation requirement |
| --- | --- |
| Notebook rendering | Python/Markdown, table HTML, text and PNG work with upstream widget/model/actions; load the editor lazily |
| CSP | Set a nonce on Typestyle's actual `typestyle/lib` instance before loading the widget, and supply CodeMirror's CSP nonce extension; allow layout style attributes and local data images; retain strict script policy |
| HTML output | Force untrusted rendering even for newly executed outputs, so pandas output style tags and notebook scripts are sanitized; omit JavaScript/SVG renderers |
| Authentication | Proxy REST and binary WS through one origin; keep upstream token server-side, validate Host/Origin/session/CSRF, consume a handshake ticket once and reject replay |
| Kernel discovery | `kernel_dirs` is not a configurable trait in the selected Jupyter client; explicitly set the private kernelspec manager's registry and disable native fallback and automatic kernel restart |
| Admission | A Jupyter manager hook can await a real A03 session before kernel launch and compensate if launch fails |
| Saving | Default contents API accepts a stale write; an expected-hash/file identity adapter is required in N03 |
| Reconnect | Reattach to the same live session by notebook path; explicitly assert a Python counter did not increment on reload |
| Restart | Shutdown and a new admission clear Python variables while retaining the notebook and querying the same current epoch |
| Interrupt | Python-only interruption is effective; a running Sail UDF did not return a Python reply within five seconds of interrupt in qualification; owned shutdown closes its A03 session |
| Protocol/outputs | v1 binary channels carry actual results; frame bounds and stream observations do not constitute aggregate output/queue enforcement |
| Ownership | Fixture-owned Jupyter plus Jupyter-owned Python demonstrates hooks; durable daemon registration, generation fencing and multi-client isolation remain N02 |

The fixture passes the complete endpoint from A03, including session parameters,
and verifies `epoch_id` in Python before displaying data. It never substitutes a
fresh Spark session with an unbound catalog. Tests use an isolated project and
native PostgreSQL table with two exact decimal amounts totaling 19.75.

The observed REST inventory is `GET api/kernelspecs`, `GET api/kernels`,
`POST api/kernels` (the failure injection), `GET/POST api/sessions`,
`PATCH/DELETE api/sessions/{id}`, `GET/PUT api/contents/orders.ipynb`, and
`POST api/kernels/{id}/interrupt`, beneath the private `/jupyter/` prefix.
Channels use `api/kernels/{id}/channels` with
`v1.kernel.websocket.jupyter.org`. The retained route report contains actual
requests. Production routing must additionally bind each ID to its console
session and daemon record; a path allowlist alone is insufficient.

CSS uses upstream theme/notebook classes plus a wrapper for fixture layout.
It is not isolated in a shadow root: theme variables and upstream rules enter
the document globally. N03 must check coexistence with the console's styles.
The component loads lazily after authentication; the fixture is a separate React
entry, so N03 still needs to measure the integrated console bundle.

## Evidence and measurements

Local Linux development qualification passed the embedded query, output, save
and shutdown path with no browser errors or external browser requests. The
extended harness also covers authentication failures, failed-start compensation,
binary WS ticket replay, reload without replay, interrupts and restart. A local
host without network isolation is not offline evidence.

The `notebook-probe` workflow builds and relocates a separate dependency archive
on Linux x86_64 and macOS arm64 and runs the installed baseline/packaged kernel
with external network access denied. Each target retains:

- `notebooks.json`: native runtime, package/identity, lifecycle and memory report.
- `notebooks.browser.json` and PNG: browser checks, timing and observations.
- `datalayer.json` and build log: exact rejected candidate behavior.
- `probe.json`: inventory, source identity, package licenses and native loaders.
- Archive SHA-256 and component size comparison.

Both jobs passed in [native notebook run 34380444801](https://github.com/supabricks/platform/actions/runs/34380444801)
at PR head `5cf135110f8f984bbae2109862a4f5cf78cb32d2` (the checkout merge identity
is retained in each report). Each target passes 11 browser scenarios and four
native checks, with zero browser errors or external browser requests. Exact
measurements, archive hashes, candidate rejection, route inventory and ownership
events are checked in as [Linux evidence](../../e2e/native/notebooks/evidence/linux-x86_64.json)
and [macOS evidence](../../e2e/native/notebooks/evidence/macos-arm64.json).

| Measurement | Ubuntu 24.04 x86_64 | macOS 15 arm64 |
| --- | --- | --- |
| Jupyter startup | 2.23 s | 3.75 s |
| Jupyter idle RSS | 71.29 MiB | 82.66 MiB |
| Kernel start to first reply | 5.24 s | 6.99 s |
| Kernel idle RSS | 172.34 MiB | 169.33 MiB |
| First notebook run | 0.24 s | 0.42 s |
| Frontend assets | 3.07 MiB | 3.07 MiB |
| Compressed probe archive | 441.95 MiB | 238.22 MiB |
| Compressed component delta | 18.14 MiB | 17.13 MiB |

Measurements are single CI observations, not performance guarantees. Startup
uses a fresh server/kernel process; builder and OS caches are not flushed.
Kernel timing includes admission and waits for an actual kernel-info reply;
idle RSS is sampled through bundled psutil before user cells execute. Server RSS
excludes the bridge, Sail and PostgreSQL. The component delta compares equivalent
baseline Python/libraries/notices and the candidate at gzip level 1; the standalone
probe deliberately duplicates the baseline environment. It is not a measured
increase to the final installer. Artifact hashes identify these exact builds,
not byte-for-byte reproducibility across timestamps or CI machines.

Both targets observe 262,160 bytes of stdout in one stream message and the
five-second Spark interrupt timeout followed by successful owned shutdown.
All three admitted analytical sessions (including the injected failed start)
are closed, both launched kernels exit, and the saved notebook validates with
nbformat. macOS additionally verifies the targeted ZeroMQ library relocation.
The test workspace uses a short `/tmp` root to fit PostgreSQL Unix socket paths.
A subsequent rerun timed out without a browser checkpoint. The hardened harness
retains progress after every scenario and bounds stalled calls. It also removes
a race in the Python interrupt test by waiting for an execution marker before
sending the interrupt, rather than accepting a potentially stale busy status.

## Work carried into N02-N05

N02 must implement the production Rust bridge, daemon ownership/admission,
WebSocket revocation, explicit Spark interruption fallback, bounded queues/RSS,
expiry and recovery. N03 supplies restricted project contents, atomic/conflict
save behavior and the real console editor. N04 supplies branch/snapshot refresh
and rebind UX. N05 qualifies the final integrated installer and recovery flow.

The probe's private token and fixed file root do not sandbox Python. Notebook
source is application data, separate from data-root backups. Standard notebook
compatibility does not imply support for arbitrary widgets, magics, Spark
libraries, user-installed packages or multi-user execution.
