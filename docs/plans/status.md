# Supabricks delivery status

Reconciled 2026-09-21 through merged platform #64 (`c44fab5`), Unity Catalog #3 and console #9 (`a5d2e7f`).
UC08 is merged and qualified. UC09 is [scoped for a shared Linux server](uc09-governed-implementation.md); IAM00 feasibility is merged in #66; UC09.1 identity/login is merged in #67; UC09.2 authorization is merged in #68; UC09.3 catalog grants merged in #69; UC09.4–.7 are merged; [UC09.8 installed qualification](../architecture/uc098-governed-release.md) is implemented for review. Complete exact-archive R04 acceptance remains required before shared-profile qualification.
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
| Project packaging PK00–PK08 | PK00 merged in #46; PK01 inspection merged in #47; PK02 source packaging merged in #48; PK03 deployment identities merged in #49; PK04 plan/apply merged in #50; PK05 offline bundles and bounded initialization merged in #51; PK06 console packaging merged in platform #52 and console #5; PK07 merged in platform #53 and fully qualified on alpha.24; PK08 typed logical table-data profile merged in #54; alpha.25 failed; inherited gates now qualified on alpha.34 | [Architecture](../architecture/project-packaging.md), [research](../research/project-packaging-industry.md), [slice plan](project-packaging-implementation.md); OSS Unity Catalog local profile qualified in UC08; IAM/governed access remains separate |
| Console project creation | Merged in platform #55 and console #6 | Browser-created project with selected main database; mandatory project ownership; alpha.26 failed; workflow now qualified on alpha.34 |
| Open-source Unity Catalog | UC00 merged in platform #56, Linux/macOS qualified (14 checks each); UC01 merged in #57 with both native offline suites qualified; UC02 merged in #58 with both native catalog suites passed; UC03 merged in platform #59 and Unity Catalog #3, Linux/macOS native catalog suites passed; UC04 merged in #60 with required checks and both native catalog suites passed; UC05 merged in #61 with required checks and both native catalog suites passed; UC06 merged in platform #62 and console #7; required checks and both native catalog/browser suites passed; UC07 merged in platform #63 with required checks and both native catalog/browser/recovery suites passed, including Linux ENOSPC; UC08 merged in platform #64 (`c44fab5`) with alpha.34 exact archives qualified; UC09.0 / IAM00 merged in #66; UC09.1 identity/login merged in #67; UC09.2 merged in #68; UC09.3 merged in #69; UC09.4 merged in #70; UC09.5 merged in #71; UC09.6 revocation/audit/recovery merged in #72; UC09.7 signed-in console merged in platform #73 / console #10; UC09.8 exact-archive qualification implemented for review, with complete R04 acceptance still required | [UC00 capability report](../architecture/uc00-catalog-probe.md), [UC00–UC09 plan](unity-catalog-implementation.md): local catalog workflow first; governed access depends on IAM and isolation |
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

PK01–PK05 merged after their required merge checks and both native-cell suites
passed. Alpha.18 (#47) qualified both archives in
[run 35452363471](https://github.com/supabricks/platform/actions/runs/35452363471).
Alpha.19 (#48) also completed all 33 checks and its combined archive evidence in
[run 35453510718](https://github.com/supabricks/platform/actions/runs/35453510718),
tested merge `e94eaa66732ccd1b60073df2ac346391ffd0d59f`. It was the fully qualified predecessor before alpha.24. Alpha.20 archive qualification failed in both NE06 lifecycle
jobs: the restore fixture moved its checkout without PK03's required explicit
reattach. PK05 updated that harness; later candidates had to rerun the complete gate.
Alpha.21 also failed its complete archive gate (baseline, macOS notebook and both environment lifecycle jobs); it is not qualified.
PK05 targets alpha.22/catalog 13; its retained baseline failed in Linux database preparation and macOS bundle export. PK07 corrects retryable database startup handling and the macOS fixture's noncanonical output path; alpha.22 is not fully qualified.
PK06 targets alpha.23/catalog 13 with the extracted console packaging workflow;
it is merged, but archive qualification has failed Linux/macOS baseline checks
and the macOS console apply check. PK07 includes fixes for retryable database
startup and canonical export paths. The original console failure's cause was not
confirmed; the current alpha.34 macOS console gate has passed. Its contract is tracked in the
[PK06 contract](../architecture/pk06-console-projects.md).

The qualified browser is Chromium on both targets. Linux qualification uses
an unprivileged minimal container / loopback-only namespace; macOS uses an
Apple Silicon runner with external networking and Homebrew denied. This does
not establish a factory-clean physical laptop trial, actual reboot/power-loss
durability, or support for other browsers/operating systems.

Public hosting, publisher signing/notarization, transitive redistribution audit,
and physical-machine/reboot/power-loss qualification remain open. UC08 alpha.34 now qualifies project portability and PK08 logical table-data transfer with the complete local workflow.
Expanded logical data profiles, governed multi-user catalog/IAM/RBAC,
HA/distributed execution, upstream-only PostgreSQL, custom Scintilla and PGlite
synchronization require separately scoped work. The first local preview never
promised those enterprise capabilities.

The obsolete legacy Kubernetes UI removal PR #1 was closed without merging on
2026-09-13. The new console lives in `supabricks/console`; the legacy operator
and its existing regression gates remain separate from the native product.

PK07 alpha.24/catalog 13 is merged in [platform #53](https://github.com/supabricks/platform/pull/53)
(`87f4898`). Both native archives and the complete R04 evidence gate passed
[run 35484139360](https://github.com/supabricks/platform/actions/runs/35484139360),
tested head `6ab87e0`. This was the packaging workstream's qualified predecessor; alpha.34 supersedes it. Historical macOS reports used
an overly broad network policy and do not establish offline execution; UC08
corrects that claim and qualifies the current candidate under the strict policy.
The earlier alpha.24 harness and ownership failures were corrected and rerun.

PK08 targets alpha.25/catalog 13 with the bounded `.sbdata` PostgreSQL table-data
companion, typed schema fidelity and transactional import receipts. Native checks
passed before merge; complete alpha.25 archive qualification failed. UC08 passes the inherited
logical-data and packaging gates on alpha.34; alpha.25 itself remains unqualified. See the
[contract](../architecture/pk08-logical-data.md) and
[installed walkthrough](../handbook/project-data.md).

Console project creation follow-up is merged in platform #55 and console #6,
targeting alpha.26/catalog 13; full release qualification failed. A browser form
creates a format-2 project, provisions and selects `main`, and opens the workspace.
The console home rejects asset work; project ownership remains mandatory for all
user-created assets. See the [walkthrough](../handbook/project-creation.md).

The historical [alpha.25 run](https://github.com/supabricks/platform/actions/runs/35495263448)
and [alpha.26 run](https://github.com/supabricks/platform/actions/runs/35496334403)
both concluded failure. Alpha.25 includes macOS notebook/Spark and snapshot-export
failures; alpha.26 failed Linux assembly. Required merge checks passing does not
qualify either archive. UC08 resolves the inherited
gates on the exact alpha.34 candidate below without retroactively qualifying
either failed archive.

UC01 alpha.27 assembled both native archives, but [run 35529510028](https://github.com/supabricks/platform/actions/runs/35529510028) failed when qualification setup selected stale alpha.26 filenames. UC02 corrects the script defaults for alpha.28/control schema 14; neither historical candidate is a qualified release.

UC02 alpha.28 built both archives and passed fresh macOS, console, ingestion, notebook and package checks. [Run 35533526822](https://github.com/supabricks/platform/actions/runs/35533526822) still failed upgrade/recovery/environment lifecycle on strict component inventory compatibility, project qualification during installer upgrade, and Linux offline tracing (external destination `168.63.129.16`). UC08 resolves these inherited gates on alpha.34; alpha.28/alpha.29 remain unqualified.

UC08 alpha.34 is qualified in [combined collector run 35593587805](https://github.com/supabricks/platform/actions/runs/35593587805),
using exact archives from [35585995786](https://github.com/supabricks/platform/actions/runs/35585995786),
tested merge `a89d0484e284ca403309055939031211ac84eaaa`. The original run passed 28
jobs and failed macOS portability predecessor preparation twice. A corrected
qualification-only guard then passed all 15 portability checks and the complete
collector, with no archive changes. Linux has 16 reports / 258 top-level checks;
macOS has 14 / 245. Catalog coverage additionally includes 37 service and 11
browser scenarios per target, plus 10 Linux / 9 macOS recovery scenarios, with
zero leaked descendants. The Linux trace recorded 6,029 destinations and no
external attempts. The [UC08 evidence and retry ledger](../architecture/uc08-release-qualification.md)
records source pins, archive hashes, resource costs and the explicit historical
macOS predecessor isolation boundary. UC00–UC08's local-owner milestone is
merged and qualified. UC09 needs actual IAM and qualified execution isolation;
the [Linux-first scope and slice plan](uc09-governed-implementation.md) starts
with [UC09.0 / IAM00 feasibility](../architecture/iam00-governance-probe.md), now implemented and locally qualified. No governed multi-user profile is implemented.
