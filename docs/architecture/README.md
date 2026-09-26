# Architecture and qualification records

[Documentation home](../README.md) · [Stack overview](../stack.md) ·
[Implementation plans](../plans/README.md)

Start with the stack overview for the current architecture. These documents
preserve individual contracts, decisions and acceptance evidence. Dated probes
and intermediate slice limitations describe their original boundary; use the
[delivery ledger](../plans/status.md) for current completion status.

The latest complete release record is [SY08 / alpha.36](sy08-installed-sync.md).
Its retained reports identify exact archives; source tests or a matching version
string do not establish equivalent qualification. Machine-readable evidence
remains beside its owning record in the existing evidence directories.

## Native runtime and database

- [P04 native databases and branches](native-branches.md)
- [P03 native storage cell](native-cell.md)
- [P05 stable connections and suspension](native-connections.md)
- [P06 qualification](p06-qualification.md)

## Analytics, console and ingestion

- [SY08: exact installed sync qualification and operating envelope](sy08-installed-sync.md)
- [Local synchronization CPU scaling: methodology and measured limits](sync-core-scaling.md)
- [Synchronization workflow profile: capture I/O, reader contention and apply planning](sync-workflow-profile.md)
- [SY07: recovery, compaction and published-spool retention](sy07-sync-hardening.md)
- [SY06: shared controls and governed service authority](sy06-sync-surfaces.md)
- [SY01: managed snapshot policies and scheduling](sy01-managed-snapshots.md)
- [SY05: supervised continuous policies and observed freshness](sy05-continuous-sync.md)
- [SY04: triggered incremental policies and fixed source barriers](sy04-triggered-sync.md)
- [SY03: incremental row application and atomic version maps](sy03-incremental-epochs.md)
- [SY02: durable PostgreSQL capture and isolated bootstrap](sy02-durable-capture.md)
- [SY00: managed capture and bootstrap probe](sy00-capture-probe.md)
- [A01: frozen Postgres exports](a01-frozen-exports.md)
- [A02: atomic analytical epochs](a02-analytical-epochs.md)
- [A03: pinned Sail sessions and analytical queries](a03-analytical-sessions.md)
- [C01: packaged local project overview](c01-console.md)
- [C02: PostgreSQL database workspace](c02-workspace.md)
- [C03 — Analytical workspace](c03-analytical-workspace.md)
- [Console source ownership and integration](console-source-split.md)
- [I00: durable ingestion contracts and catalog 9](i00-ingestion.md)
- [I01: owned CSV ingestion](i01-ingestion.md)
- [I02: local browser ingestion](i02-console-ingestion.md)
- [I03: JSON and Parquet in the existing ingestion workflow](i03-ingestion.md)
- [Local console and file ingestion](local-console-ingestion.md)
- [Controlled Sail source and native builds](source-built-sail.md)

## Notebooks and environments

- [Local notebooks in the Supabricks console](local-notebooks.md)
- [N01: notebook component and kernel qualification](n01-notebook-qualification.md)
- [N02: owned local notebook runtime](n02-notebook-runtime.md)
- [NE01: separate notebook kernel environments](ne01-notebook-environments.md)
- [NE02: durable notebook environment preparation](ne02-environment-manager.md)
- [NE03: notebook kernels bound to managed environments](ne03-kernel-environments.md)
- [NE04: reproducible notebook package workflows](ne04-notebook-packages.md)
- [NE05: notebook environment controls](ne05-console-environments.md)
- [NE06 — Installed environment lifecycle qualification](ne06-environment-qualification.md)

## Project packaging

- [PK01 — Versioned project source inspection](pk01-project-inspection.md)
- [PK02 — Deterministic source packages](pk02-source-packages.md)
- [PK03 — Destination-owned deployment bindings](pk03-deployment-bindings.md)
- [PK04 — Project plan and journaled apply](pk04-project-apply.md)
- [PK05: offline project closure and initialization](pk05-offline-projects.md)
- [PK06 — console project packages](pk06-console-projects.md)
- [PK08 — PostgreSQL logical data packages](pk08-logical-data.md)
- [Portable projects, deployments and governance](project-packaging.md)

## Catalog and governed access

- [IAM00: identity and execution isolation feasibility](iam00-governance-probe.md)
- [UC00: OSS Unity Catalog feasibility probe](uc00-catalog-probe.md)
- [UC02: project-owned catalog metadata](uc02-catalog-metadata.md)
- [UC03: durable complete-set catalog publication](uc03-durable-publication.md)
- [UC04: session-pinned catalog reads](uc04-catalog-reads.md)
- [UC05: project dataset bindings](uc05-dataset-bindings.md)
- [UC06: project Data browser](uc06-console-data.md)
- [UC07: catalog recovery and operational limits](uc07-catalog-recovery.md)
- [UC09: governed on-prem scope](uc09-governed-on-prem.md)
- [UC09.1: principals and login](uc091-principals-login.md)
- [UC09.2: project policy and execution admission](uc092-authorization.md)
- [UC09.3: private UC identities and reviewed grants](uc093-catalog-grants.md)
- [UC09.4: isolated execution and admitted file closure](uc094-isolated-execution.md)
- [UC09.5 — Governed PostgreSQL and data movement](uc095-governed-data.md)
- [UC09.6 — Revocation, audit and governed recovery](uc096-revocation-recovery.md)
- [UC09.7: signed-in console workflows](uc097-governed-console.md)

## Release qualification and recovery

- [R01 localhost installer qualification](r01-qualification.md)
- [R02: complete local analytical preview](r02-analytical-preview.md)
- [R03: stopped-cell recovery and explicit platform upgrades](r03-recovery-upgrades.md)
- [R04 — Complete local workflow qualification and handoff](r04-local-release.md)
- [UC08: qualify the complete installed local catalog product](uc08-release-qualification.md)
- [UC09.8 — Installed governed release qualification](uc098-governed-release.md)

## Synchronization performance

- [SP00: reproducible comparisons and measured baseline](sp00-reproducible-comparisons.md)
- [SP01: safe journal-read contention recovery](sp01-journal-contention-recovery.md)
- [SP02: bounded durable capture groups](sp02-durable-capture-groups.md)
- [SP03a: packaged SQLite qualification](sync-performance-sp03a.md)
- [Measurement and long-run profiling contract](sync-performance-comparisons.md)
