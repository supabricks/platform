# Console source ownership and integration

The split starts from PR #33 at `3d51a05c3d3df0ce67131e09ba8759720ea17252`,
including N06, the holistic notebook repairs and the macOS qualification fixes.
It is implemented in a separate platform worktree/branch, so PR #33 can continue
through review and qualification independently.

## Ownership

| Component | Owner |
| --- | --- |
| React application, JupyterLab editor, browser API client, styles | `supabricks/console` |
| npm locks, Vite build, asset inventory and frontend license texts | `supabricks/console` |
| Product browser scenarios and their synthetic CSV fixture | `supabricks/console` |
| Authentication, HTTP/WebSocket bridge, notebook files, process ownership | `supabricks/platform` |
| PostgreSQL/Sail/Jupyter runtime, ingestion service, install/upgrade/backup | `supabricks/platform` |
| Exact-archive/native/offline qualification and notebook fault injection | `supabricks/platform` |
| Legacy Kubernetes `ui/` application | Remains in `supabricks/platform` |

The console repository was extracted with `git subtree split --prefix=console`
to preserve the frontend's history. It builds independently with Git, Node and
npm. Its browser tests accept a runtime binary explicitly; no test reads files
from a parent platform checkout. The combined notebook harness now lives at
`e2e/native/notebooks/qualify-product.mjs` in platform and invokes both the
runtime fault tests and the console repository's product tests.

## Pinned source, packaged assets

Platform retains the `console/` path as a Git submodule. Its gitlink pins the
reviewed source commit; `.gitmodules` names the repository. No moving branch or
latest npm package selects the shipped UI. Native release CI initializes the
submodule before dependency installation and offline qualification.

```bash
git submodule update --init console
npm ci --prefix console
npm run build --prefix console
```

Build emits API-1 `console.json` and inventoried assets, plus
`build/console-source.json` containing the source commit, dirty flag, npm lock
hash and asset-manifest hash. Platform assembly verifies the checkout matches its gitlink and the
built source record matches that clean checkout. The release includes source
identity, package locks and notices. Installed users receive static assets in
`share/console`; they need neither Git nor Node and perform no submodule fetch.

For a UI change, commit/test it in console, then update the platform gitlink in
a PR. A pin-only change triggers native release qualification. API changes
must coordinate both repositories and preserve the existing version check.
Dirty or stale source builds fail assembly; local development can still build
and serve assets before committing.

## Browser delivery boundary

The application continues to use the local bridge's same-origin `/api/`, launch
exchange, session cookie, CSRF checks, WebSocket tickets and stylesheet nonce.
Source extraction does not alter these contracts. Reuse on `supabricks.io`
will require a separately designed hosted-to-local connection/authentication
mechanism. The on-prem installation remains the supported delivery path.
