# Supabricks Platform

Supabricks combines branchable PostgreSQL 17.8, Sail/Spark SQL over immutable
Delta snapshots, file ingestion, browser notebooks with managed Python
environments, portable projects and open-source Unity Catalog. The native
application serves local-owner workflows on Linux/macOS and an explicitly
qualified governed shared-server profile on Linux.

**Start with the [documentation home](docs/README.md) and
[what we have built](docs/stack.md).** They connect the stack architecture,
implementation plans, operating guides and release evidence in one place.

| Start here | Purpose |
| --- | --- |
| [Stack overview](docs/stack.md) | Capabilities, data flow, deployment profiles and source ownership |
| [Local walkthrough](docs/handbook/local-demo.md) | Install, import, query, branch and run a notebook |
| [Governed server](docs/handbook/governed-server.md) | Set up TLS/OIDC, isolated execution and governed access |
| [Handbook](docs/handbook/README.md) | User and operator workflows |
| [Implementation plans](docs/plans/README.md) | All workstreams, including IAM00 and UC09.1–UC09.8 |
| [Delivery ledger](docs/plans/status.md) | What shipped, what remains and historical release outcomes |
| [Architecture and evidence](docs/architecture/README.md) | Subsystem contracts and acceptance records |
| [Build and install](install/native/README.md) | Assemble and run the native stack |

UC00–UC09 is complete for its planned profiles. Alpha.35 passed the full
Linux/macOS local-owner matrix and Linux governed acceptance; the
[qualification ledger](docs/architecture/uc098-governed-release.md) identifies
exact tested archives and limitations. A new local build needs its own release
qualification before it can enable shared ingress.

The local-owner installation bundles Python and Java and needs no target-machine
Docker or Kubernetes. Governed execution has additional operator prerequisites.
Public delivery at `supabricks.io` remains deferred. Code is
[Apache 2.0](LICENSE); bundled dependencies retain their licenses.

## Source layout

- [crates/local](crates/local/src/): native CLI/MCP, daemon, state, database and analytical workflows, catalog, identity and governed execution.
- [crates/core](crates/core/README.md): shared compute configuration, identities and validation.
- [console](console/): frontend source pinned from [supabricks/console](https://github.com/supabricks/console). Initialize with `git submodule update --init console` before building.
- [python](python/): analytical, ingestion and notebook integration.
- [components](components/README.md): source locks, native build inputs and provenance.
- [install/native](install/native/README.md): release assembly, installer and qualification tooling.
- [e2e/native](e2e/native/README.md): installed native acceptance suites.
- [docs](docs/README.md): the stack's technical documentation home.

The earlier Rust operator and Helm chart remain in `crates/operator`, `chart/`
and their existing regression suites. Their [Kubernetes quickstart and limits](docs/handbook/kubernetes-profile.md)
are documented separately from the native product.
