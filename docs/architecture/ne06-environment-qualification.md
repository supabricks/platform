# NE06 — Installed environment lifecycle qualification

NE06 completes the planned environment qualification layer for the localhost
preview. The release is `v0.1.0-alpha.13`, catalog 10; console source stays pinned
to NE05. Completion requires passing final-revision CI evidence on both targets.
No deployment or public release publication is part of this slice.

## Exact product lifecycle

`release-environment-lifecycle` downloads the assembled candidate, the retained
NE05 alpha.12 archive from run `34733919344`, and an explicit package bundle
produced by the candidate's real `release-packages` job. Archive checksum and
installation verification precede use. The harness uses the bundled Python and
real authenticated console/Jupyter channels, PostgreSQL and Sail.

The predecessor and candidate are served through a signed localhost
`curl | bash` installer in a private prefix containing spaces and a quote. Linux
runs unprivileged in a loopback-only network namespace. macOS runs with Seatbelt
denying external networking and Homebrew. Product subprocesses use a fresh HOME,
empty language-tool configuration and an OS-only PATH. No release files or
catalog records are patched for this lifecycle test.

The test starts an offline base notebook, prepares pure/native dependencies,
exports a bundle and saves output provenance. It upgrades while a real kernel
holds an environment lease, verifies shutdown and backup, retains the old
interpreter, rejects the old generation under the new installation, and executes
only after explicit preparation and selection. It checks lease-aware collection,
explicit adoption with unchanged epoch, bundle contract/target rejection and
down/up behavior.

For cold recovery it backs up durable platform data, restores into a new data
root and moves the project while preserving its identity and saved notebook.
The fixture includes boltons, which is absent from the release wheelhouse.
Offline sync must fail without its artifacts; explicit bundle import must build
a new environment and execute the preserved Sail snapshot. The source data root
and its venv remain present so accidental cross-root reuse/deletion is detectable.

## Deterministic resolver faults

`index.py` serves a controlled loopback package index containing hash-pinned
pure/native wheels from the archive. It exercises the exact bundled uv and
Python: resolution, hashed installation, dependency conflict, corrupt expected
hash, source-only dependency refusal without build execution, HTTP 503 and cold offline behavior with zero index requests.

This is a lower-level resolver probe. It does not bypass the product's registry
validation or add custom-index configuration. The real product add/remove and
download path remains covered by `release-packages` and the 16-check NE05 browser
suite, which explicitly access PyPI. The lifecycle and existing base/kernel
suites prove offline product behavior with OS-enforced external-network denial.

## Evidence gate

`environment-evidence` depends on the existing Postgres/app, analytics, console,
ingestion, notebook, environment, package and physical recovery jobs, plus the
new lifecycle matrix. Those jobs must succeed in the same workflow run.
`environment_evidence.py` additionally rejects missing/incomplete reports,
failures, cleanup errors, dirty source or mixed release identities across the
environment and browser suites. Browser installer reports now record the
verified installed release identity and manifest digest.

The `ne06-evidence` artifact records each target's archive digest, manifest and
kernel contract identities, platform/console/engine/helper source pins, Python
version, shipped and project-bundle wheel hashes, notices, measured costs and constituent report digests.
`platform_commit` must equal that workflow's checked-out revision (GitHub's PR
merge revision during PR validation). A new push requires new evidence; a local
resealed engineering derivative is useful for debugging but cannot satisfy this
gate.

Retrieve a completed run with:

```sh
gh run download RUN_ID --repo supabricks/platform --name ne06-evidence --dir evidence
gh run download RUN_ID --repo supabricks/platform \
  --name release-environment-lifecycle-linux-x86_64 --dir evidence/linux
gh run download RUN_ID --repo supabricks/platform \
  --name release-environment-lifecycle-macos-arm64 --dir evidence/macos
```

GitHub artifacts have retention limits. Before the predecessor expires, retain
its verified archive and checksum in the release archive or update the pinned
predecessor to a fully qualified equivalent through review. This preview does
not provision a public distribution service or publisher signing keys.

## Qualification boundaries

The existing NE02 manager fault suite and NE03 real-kernel suite rerun against
the candidate archive: cancellation, stale inputs, artifact corruption,
process ownership, daemon crash recovery, lease release and no cell replay.
Rust contracts retain the individual partial-publication/recovery boundary tests.
The physical recovery suite retains the exact catalog-9 predecessor migration
and interruption probes. NE06 adds the same-catalog upgrade with environments.

The qualified upgrade retains Python 3.12.13, dependency inventories, engine and
target. Wrong target/component bundles and old installation generations must
fail. Cross-Python dependency migrations, engine major upgrades, power loss,
kernel reboot, arbitrary native-wheel portability, hostile-code sandboxing and
Sail UDF dependency propagation are not claimed. Measured costs describe the
runner, not a universal performance guarantee. The [user guide](../handbook/notebook-environments.md)
and [package example](../../examples/notebooks/environments.md) describe the
supported recovery workflow.
