# R04 — Complete local workflow qualification and handoff

R04 merged in platform #44 and closes the console/ingestion release slice after platform #43 and console #4
(C03), with I03 and N00–N06 / NE01–NE06 already merged. The qualified release is
`v0.1.0-alpha.16`, catalog 10, PG17.8, with the reviewed `supabricks/console`
submodule. No execution engine, UI rewrite, runtime dependency or catalog
migration is needed for this slice.

## Remaining work and implementation

The prior slices already assemble both native archives, install them through
signed localhost curl, qualify real CLI/MCP/browser workflows offline, and test
COPY commit reconciliation, recovery, notebooks and managed environments. C03
qualified 39 browser and 39 ingestion checks on each alpha.15 target in
[run 34743055512](https://github.com/supabricks/platform/actions/runs/34743055512).
That is predecessor evidence, not qualification of a changed R04 archive.

R04 adds a release-wide evidence collector because the previous NE06 collector
only correlated the environment/notebook reports. It required one console check
and did not compare ingestion, recovery, baseline and benchmark identities.
`install/native/release_evidence.py` now requires those reports together, rejects
failed cleanup, partial/duplicate browser checks, missing metrics/fixture hashes,
stale console or worker sources, and mixed release identities. It retains the
existing environment gate and check name, extends it to R04, and emits bounded
JSON plus a Markdown handoff as the `r04-evidence` artifact and job summary.

The collector binds both native targets to the reviewed platform revision,
console gitlink, ingestion worker checksum, immutable release manifest and
archive checksum. It records browser versions, asset/lock hashes, notice inventory
hash/count, shipped demonstration hashes, original report hashes and bounded
resource measurements. It does not copy SQL results, credentials, user sources,
private paths or process diagnostics into the handoff. The notice inventory
records what ships; it does not replace the separate public redistribution audit.

The installed `DEMO.md`, `examples/console/sales.csv` and unexecuted
`sales.ipynb` provide an offline walkthrough without a checkout or development
server. The installed browser gate verifies these against the immutable file
inventory, and verifies the sales fixture and notebook query match the qualified
C03 scenario. Existing browser and notebook tests exercise that workflow against
real PostgreSQL and Sail. Documentation now leads with the native product and
states that Chromium is qualified on both targets; a Mac user should use
`console --no-open` instead of implicitly selecting unqualified Safari.

Finally, release harness cleanup failures must fail qualification and preserve
failed fixtures for inspection. Successful execution followed by failed shutdown
cannot produce a passing release report.

## Acceptance coverage

| Requirement | Exact-archive gate / artifact |
| --- | --- |
| Signed localhost install; no builder tools needed by runtime; CLI/MCP and app flow | `clean-linux`, `isolated-macos`; `release-qualification-*` |
| PostgreSQL, browser import, Spark SQL, epoch isolation and session cleanup | `release-console-*`: at least 39 checks, installed demo inventory and Chromium version |
| CSV/TSV/JSON/JSONL/Parquet limits, COPY boundaries, receipt reconciliation, worker death, import backup/restore and resource limits | `release-ingest-*`: at least 39 checks and synthetic fixture/resource evidence |
| Both migration/activation sides, worker/daemon deaths and retained data restoration | `release-recovery-*`: at least 18 checks |
| Browser notebook, kernel/environment identity, offline wheels, package operations, upgrades and cold restore | Existing NE06 environment collector: eight reports / 140 checks per target including console |
| No external network attempts in the Linux baseline | Traced descendants and passing `network.json`; macOS records Seatbelt scope |
| Snapshot performance and resource context | 10 MB, 100 MB and 1 GB snapshot benchmark reports; sampled metrics, not general capacity guarantees |
| Source/manifest/worker/console agreement, bounded reports and whole-workflow completeness | Extended `environment-evidence` job; `r04-evidence.json` and `.md` |
| Existing regressions | Portable, native baseline/cell, full Rust/operator unit and Kubernetes e2e gates retained |

Linux is an unprivileged minimal Ubuntu 24.04 container / loopback-only namespace.
macOS is a separate Apple Silicon runner with external networking and Homebrew
denied, not a factory-clean physical Mac. Node/Playwright and host Python are test
drivers installed before isolation; the product supplies its own Python, wheels,
processes and compiled frontend. No model account is part of the acceptance gate.
Browser automation is pinned by the console lockfile; no drivers are downloaded
by the installed application. Safari/Firefox, Intel Mac, Linux arm64, musl,
Windows and forwarded/remote browser transport remain unqualified.

## Exit and follow-ups

Completion requires all mandatory checks on the reviewed candidate, including
both exact archive reports and the final identity collector. PR evidence records
the run, tested revision, manifest identities and actual measurements. The
[installed walkthrough](../handbook/local-demo.md) is the user entry point;
metrics are emitted from the actual candidate, rather than copied from a prior
release. Documentation is a handoff for the tested flow, not a claim of a separate
human usability study.

This completed the localhost console/ingestion workstream.
Final qualification: [run 34762457678](https://github.com/supabricks/platform/actions/runs/34762457678),
29 passing CI checks and matching exact-archive evidence on both targets. V01
(thin VS Code integration) and L01 (direct analytical dataset design) remain
optional next slices. Project packaging, shared catalog/IAM, and hosted console delivery require their own scope. The
[source-built Sail follow-up](source-built-sail.md) is now implemented in PR #45
and undergoing its own exact-archive qualification. Public domain deployment,
publisher signing/notarization, redistribution audit and physical power-loss
qualification retain their separate gates.


## PK07 project portability extension

Alpha.24 keeps this collector as the sole combined release authority. Each native
assembly exports its base wheel closure through the installed CLI. One Linux job
packs both target closures into one deterministic `sales.sbproj`. Both destination
jobs download those exact bytes; independently rebuilding a similar package is
rejected. The producer records the canonical packaged notebook/fixture hashes,
not hashes of pre-stripping source text.

`project_evidence.py` supplies strict validation to `release_evidence.py`:
producer/archive/manifest identities must agree with retained lifecycle evidence,
closures must match each native kernel contract and the common declaration pair,
and both destination reports must identify the same actual artifact. Required
checks are named, not a count that unrelated checks can satisfy. Missing checks,
failed cleanup, missing isolation, zero/non-finite measurements and mixed reports
fail the combined gate.

The destination first deploys into a clean candidate installation. A separate
private installation seeds the alpha.22 predecessor's installed project, upgrades
through the signed localhost installer, and explicitly reapplies the transferred
candidate package to that deployment. This allows legitimate changes to native
kernel contracts without pretending an old bundle is compatible or rewriting its
provenance. Cold restore then rebuilds from the same candidate source artifact.
Alpha.22 is an exact pinned upgrade input, not a claim that all its archive gates
passed. Only predecessor seeding allows one explicit reapply after its known
premature database-preparation failure: the sole retained database must reconcile,
no revision may have activated, and no later resource may have been prepared.
The report records this recovery; candidate applies receive no such retry. Its retained baseline failures are corrected in PK07: retryable database
startup stays pending, and the macOS bundle-export fixture uses canonical parents.

Package/expanded bytes describe the transferred artifact including its metadata;
latencies describe first candidate apply and kernel readiness. Resource sampling
covers daemon descendants and allocated data-root bytes, excluding installation,
harness and archive staging. It can miss short peaks and double-count shared RSS.
The two-row fixture is not a general capacity qualification. Existing R04 browser,
ingestion, notebook, storage, recovery and benchmark suites remain mandatory.
