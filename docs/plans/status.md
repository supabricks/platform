# Supabricks delivery status

Reconciled 2026-09-13 against merged platform #45 (`916a793ab6d57780be0c57b54312ff1884e88bcd`).
This page records delivered scope. Older dated design sections describe their
starting point and original acceptance criteria, not the current backlog.

| Workstream | Delivered state | Evidence / current boundary |
| --- | --- | --- |
| P00 / E01 / P01–P06 | Merged: PG17.8/Neon native engine, local state/supervision, branches, connections, CLI and MCP | Native engine, runtime and installed application gates retained |
| A00–A03 | Merged: frozen exports, atomic Delta/Parquet epochs and Sail sessions | Read-only analytical SQL over explicit snapshots; no continuous CDC |
| R01–R03 | Merged for localhost: installer, complete offline runtime, stopped-cell backup/restore and upgrades | Public delivery and physical failure qualification remain separate |
| C01–C03 / I00–I03 | Merged: separate console, PostgreSQL/Spark workspaces and device file ingestion | CSV/TSV, JSON/JSONL/document and Parquet; new PostgreSQL destinations |
| N00–N06 / NE00–NE06 | Merged, including holistic repairs: browser notebooks and managed project environments | Separate kernels, dependency locks, package controls, offline bundles and cold restores |
| R04 | Merged in #44: combined local release evidence and installed demo | Both alpha.16 archives qualified; 29 CI checks, 39 browser and 39 ingestion checks per target |
| Controlled Sail source build | Merged in platform #45; both alpha.17 archives qualified, 33 final CI checks passed | `supabricks/sail` at the reviewed 0.7.1 commit; deployed Sail wheels are built from source |
| Project packaging PK00–PK08 | PK00 proposal/research documented; implementation not started | [Architecture](../architecture/project-packaging.md), [research](../research/project-packaging-industry.md), [slice plan](project-packaging-implementation.md); UC/IAM remain separate follow-ons |
| W01 / hosted console | Deliberately deferred | No production `supabricks.io/install.sh` or hosted console transport yet |
| V01 / L01 | Optional follow-ons | Thin VS Code integration and direct analytical datasets are not prerequisites for the delivered local preview |

[R04 run 34762457678](https://github.com/supabricks/platform/actions/runs/34762457678)
qualified tested merge `1f7f844ab9aa87579e0233fa7ddf8166b612ef2b`, console
`70af6a2fd2cf17b3a93cbc1bfb72025b2892cc60`, catalog 10 and PG17.8. Its
`r04-evidence` artifact records 13 reports / 212 checks on Linux x86_64 and
11 reports / 204 checks on macOS arm64. These are checks across suites, not
counts of distinct product features. It is predecessor evidence for alpha.17;
a changed release must pass its own complete archive gates.

Alpha.17 [run 34767244500](https://github.com/supabricks/platform/actions/runs/34767244500)
qualified tested merge `f2812627ad6004c88565a73afb7599c652724b4f`, retaining the
console pin and report/check totals above. Its 33 checks passed after an unchanged
macOS environment-console retry; the original offline-import timeout and duplicate
report-artifact cleanup are recorded in [PR #45](https://github.com/supabricks/platform/pull/45).

The qualified browser is Chromium on both targets. Linux qualification uses
an unprivileged minimal container / loopback-only namespace; macOS uses an
Apple Silicon runner with external networking and Homebrew denied. This does
not establish a factory-clean physical laptop trial, actual reboot/power-loss
durability, or support for other browsers/operating systems.

Public hosting, publisher signing/notarization, transitive redistribution audit,
and physical-machine/reboot/power-loss qualification remain open. Full project
packaging beyond notebook dependency bundles, shared catalog/IAM/RBAC,
HA/distributed execution, upstream-only PostgreSQL, custom Scintilla and PGlite
synchronization require separately scoped work. The first local preview never
promised those enterprise capabilities.

The obsolete legacy Kubernetes UI removal PR #1 was closed without merging on
2026-09-13. The new console lives in `supabricks/console`; the legacy operator
and its existing regression gates remain separate from the native product.
