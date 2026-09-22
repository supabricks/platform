# Supabricks handbook

[Documentation home](../README.md) · [Stack overview](../stack.md) ·
[Implementation plans](../plans/README.md)

Use this handbook to run and operate the delivered product. For completion status
and exact release qualification, use the [delivery ledger](../plans/status.md).

## Start and operate the stack

| Guide | What it covers |
| --- | --- |
| [Local walkthrough](local-demo.md) | Install and demonstrate import, SQL, snapshots, branching and notebooks |
| [Local console](local-console.md) | Launch the browser console, source development and session recovery |
| [Local CLI and MCP](local-workflow.md) | Project selection, commands and agent workflows |
| [Governed Linux server](governed-server.md) | TLS/OIDC, administration, isolated execution, audit and recovery |
| [Recovery and upgrades](recovery.md) | Stopped backups, new-root restore and supported upgrades |
| [Local state](local-state.md) | SQLite control state and durable operations |
| [Build and installation](../../install/native/README.md) | Native assembly, verified installation and release checks |

## Projects and portability

| Guide | What it covers |
| --- | --- |
| [Create projects](project-creation.md) | Browser setup and mandatory project ownership |
| [Project inspection](project-inspection.md) | Offline source validation and resource graphs |
| [Source packages](project-packages.md) | Export, verify and unpack deterministic `.sbproj` packages |
| [Deployment bindings](project-deployments.md) | Independent runtime identities, attach/adopt and source forks |
| [Reviewed plan/apply](project-apply.md) | Review changes, retain resources and apply immutable revisions |
| [Offline runnable projects](project-offline.md) | Target dependencies, migrations and explicit fixtures |
| [Portability walkthrough](project-portability.md) | Package and reopen a project on another installation |
| [Logical table data](project-data.md) | Bounded `.sbdata` export and transactional import |

## Data, analytics and notebooks

| Guide | What it covers |
| --- | --- |
| [Database workspace](database-workspace.md) | Branches, SQL, cancellation and saved queries |
| [File ingestion](file-ingestion.md) | CSV/TSV, JSON/JSONL/document and Parquet |
| [CSV/TSV ingestion](csv-ingestion.md) | CLI/MCP mappings and durable import jobs |
| [Browser imports](browser-imports.md) | File selection, approval and import recovery |
| [Managed analytical sync](managed-sync.md) | Console/agent controls, reviewed resync and governed snapshot service authority |
| [Analytical workspace](analytical-workspace.md) | Snapshot publication, pinned Spark SQL and bounded comparison |
| [Notebooks](notebooks.md) | Project files, editor, Sail kernels and snapshots |
| [Notebook environments](notebook-environments.md) | Dependency locks, packages, offline bundles and environment recovery |

## Unity Catalog

| Guide | What it covers |
| --- | --- |
| [Catalog walkthrough](catalog-demo.md) | Publish, discover and consume data across two projects |
| [Catalog service](catalog-service.md) | Managed service lifecycle, readiness and configuration |
| [Metadata browser](catalog-metadata.md) | Catalog discovery, identities, schema and freshness |
| [Publication](catalog-publication.md) | Durable publication of explicit snapshots |
| [Dataset bindings](catalog-datasets.md) | Cross-project consumption and pinned data |

## Earlier Kubernetes profile

These guides describe the operator/Helm prototype. Its architecture, open-mode
MCP authentication and deferred features must not be applied to the native or
governed profile.

- [Kubernetes quickstart and limits](kubernetes-profile.md)
- [Kubernetes architecture](architecture.md)
- [Kubernetes development loop](dev-loop.md)
- [Kubernetes runbook](runbook.md)
- [Kubernetes backlog](backlog.md)

For subsystem implementation and acceptance suites, use the
[architecture index](../architecture/README.md) and [review records](../reviews/README.md).

The [SY02 local durable capture workflow](durable-capture.md) enrolls an explicit
local capture generation. [SY03 incremental epochs](incremental-epochs.md) adds
explicit bounded application and publication for local readers.

[SY04 triggered sync](triggered-sync.md) adds local CLI/API run-to-boundary and
UTC interval scheduling over an explicitly enrolled capture.

[SY05 continuous sync](continuous-sync.md) adds automatic catch-up, observed
freshness and pause at a complete batch boundary.
