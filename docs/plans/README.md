# Implementation plans

[Documentation home](../README.md) · [Stack overview](../stack.md) ·
[Current delivery status](status.md)

This is the index of implementation plans for the complete stack. Plans retain
the original slice definitions and acceptance criteria; the delivery ledger owns
current status and the architecture records retain exact qualification evidence.
Historical baseline statements and failed release candidates stay historical.

## Delivered workstreams

| Plan | Slice IDs | Delivered scope |
| --- | --- | --- |
| [Native runtime](local-runtime-implementation.md) | P00, E01, P01–P06, A00–A03, R01–R03 | Native PostgreSQL cell, CLI/MCP, connections, snapshots, analytical sessions, installer and recovery |
| [Console and ingestion](console-ingestion-implementation.md) | C01–C03, I00–I03, R04 | Browser workspaces, file ingestion and combined installed-release acceptance |
| [Notebooks](notebook-implementation.md) | N00–N06 and repair follow-ups | Embedded notebook editor, project files, kernels and source extraction |
| [Managed notebook environments](notebook-environments-implementation.md) | NE00–NE06 | Project dependency locks, managed environments/packages, offline bundles and recovery |
| [Project packaging](project-packaging-implementation.md) | PK00–PK08 | Inspection, source packages, deployments, reviewed apply, offline execution and logical table-data transfer |
| [Unity Catalog](unity-catalog-implementation.md) | UC00–UC09 | Managed OSS catalog, durable publication, pinned reads, cross-project bindings, recovery and governed access |
| [Governed server](uc09-governed-implementation.md) | UC09.0 / IAM00, UC09.1–UC09.8 | Identity, authorization, catalog grants, isolated execution, governed data, revocation/audit and signed-in console |

All listed workstreams are merged within their stated profiles. Alpha.35's
[complete qualification](../architecture/uc098-governed-release.md) is the current
retained evidence for the combined local product and the Linux governed profile.
This does not retroactively qualify failed predecessor archives.

## Planned workstreams

| Plan | Slice IDs | Proposed scope |
| --- | --- | --- |
| [Managed analytical synchronization](analytical-sync-implementation.md) | SY00–SY08 | PostgreSQL → analytics: managed snapshot scheduling, incremental triggered and continuous modes; recovery, governance and installed qualification. Reverse sync is later scope. |

SY00–SY08 are not implemented. Snapshot analytics remains the delivered baseline;
managed internal change capture is compatible with **no user-managed CDC/ETL**.

## IAM00 and UC09 navigation

IAM00 is **UC09.0**, the governance/identity/isolation probe. It is not a missing
parallel implementation plan. Its follow-on work is in the governed-server plan.

| Slice | Contract and evidence |
| --- | --- |
| UC09.0 / IAM00 | [Governance capability probe](../architecture/iam00-governance-probe.md) |
| UC09.1 | [Principals and login](../architecture/uc091-principals-login.md) |
| UC09.2 | [Project and execution authorization](../architecture/uc092-authorization.md) |
| UC09.3 | [UC identities and catalog grants](../architecture/uc093-catalog-grants.md) |
| UC09.4 | [Isolated execution](../architecture/uc094-isolated-execution.md) |
| UC09.5 | [Governed data access](../architecture/uc095-governed-data.md) |
| UC09.6 | [Revocation, audit and recovery](../architecture/uc096-revocation-recovery.md) |
| UC09.7 | [Signed-in console](../architecture/uc097-governed-console.md) |
| UC09.8 | [Installed release qualification](../architecture/uc098-governed-release.md) |

## Related records and remaining work

- [Delivery ledger](status.md): merged PRs, exact release evidence, failures and remaining scope.
- [Architecture index](../architecture/README.md): contracts for individual slices and source-build follow-ups.
- [Repository investigation](repository-map.md): historical ownership/source baselines; current ownership is in the [stack map](../stack.md#components-and-source-ownership).
- [Review and repair records](../reviews/README.md): defects and their follow-up evidence.

W01/public delivery and hosted console transport are deferred; V01/editor
integration and L01/direct analytical datasets remain optional follow-ons.
Additional capabilities in the plans are proposals until a separately scoped
implementation and qualification record says otherwise.
