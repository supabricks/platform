# N01: notebook component and kernel qualification

*Status: In qualification · Date: 2026-09-09*

N01 implements the isolated [notebook probe](../../e2e/native/notebooks/README.md)
from the [implementation plan](../plans/notebook-implementation.md). It uses the
qualified I02 alpha.8 release from native-release run `34301230539`; the shipped
console, runtime formats and analytical package pins are unchanged. Notebooks
are not enabled in the product by this PR.

## Component decision

Select a thin React adapter over upstream JupyterLab's notebook widget for the
first Supabricks notebook. Keep Jupyter Server and ipykernel as the native Python
execution stack. Final adoption requires the two native CI reports below.

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

Native CI results and measured values will be recorded here after qualification.
Do not interpret this pending evidence section as a completed N01 exit gate.

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
