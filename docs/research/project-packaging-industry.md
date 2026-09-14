# Project packaging, catalogs and identity: industry research

Researched 2026-09-13. Primary documentation and the pinned Supabricks source
were inspected. This is a design comparison, not an integration benchmark.
[Proposed architecture](../architecture/project-packaging.md) ·
[Implementation plan](../plans/project-packaging-implementation.md).

## Findings and implications

| Reference | Observed pattern | Proposed Supabricks application |
| --- | --- | --- |
| Databricks bundles | Source files and resource declarations are deployed together to named targets | A versioned project definition, immutable package and explicit target deployment |
| Databricks Unity Catalog | Catalogs organize governed data; workspaces provide execution context | Separate catalog bindings from project membership and placement |
| Databricks identities | Deployment and execution can use different identities | Record author, deployer and effective runtime principal separately |
| Snowflake Native Apps | Applications request logical references to consumer objects | Bind package requirements to destination data and secrets without embedding credentials |
| dbt | Project configuration, connection profiles and generated graph artifacts have different responsibilities | Keep portable source, private target configuration and resolved deployment state separate |
| OCI artifacts | A registry can distribute content other than container images | Add registry transport later without requiring containers to execute a project |

These applications are our recommendations. They are not claims that another
vendor implements the proposed Supabricks model.

## Databricks: project delivery and data governance are separate systems

Databricks now calls Asset Bundles **Declarative Automation Bundles**. They
combine code, notebooks and resource configuration with a CLI-driven development
and deployment workflow. This is a useful reference for a project as a complete
software deliverable. The documented target is a Databricks workspace; it does
not establish a self-contained offline local runtime. Supabricks should preserve
its native local execution advantage while adopting the declarative lifecycle.
[Bundle overview](https://docs.databricks.com/aws/en/dev-tools/bundles).

Bundle deployment tracks resources by recorded identities rather than matching
human names. The default deployment identity depends on the workspace root path,
which includes the deployer, bundle name and target. Explicit binding adopts an
existing resource into deployment management. These semantics highlight two
requirements for us: a copied project must not accidentally take over an existing
deployment, and adoption must be distinct from creating a new instance. We
recommend UUID deployment identities with explicit adoption and retaining data
when declarations disappear, rather than copying Databricks' removal behavior.
[Deployment identity and bind commands](https://docs.databricks.com/aws/en/dev-tools/cli/bundle-commands).

Development and production modes apply different default behaviors. Supabricks
can offer target defaults, but a target named `prod` must never itself confer
privileges or imply tested production isolation.
[Deployment modes](https://docs.databricks.com/aws/en/dev-tools/bundles/deployment-modes).

Databricks separates the deployer from the workflow's `run_as` user or service
principal. Its documented support differs by resource type when those identities
differ. Our execution identity must similarly be authorized explicitly; a
manifest string cannot authorize impersonation.
[Run identity](https://docs.databricks.com/aws/en/dev-tools/bundles/run-as).
Bundle resource permissions are also configurable independently of the data
catalog's grants. A right to run a workflow is therefore a different design
concern from permission to read its data.
[Bundle permissions](https://docs.databricks.com/gcp/en/dev-tools/bundles/permissions).

Unity Catalog guidance favors account-level identities, IdP-managed groups,
group ownership of production objects and service principals for automation.
Catalogs are a primary data isolation boundary; their layout may reflect an
environment, team, business unit or combination. There is no general rule that
every code project must own exactly one catalog. Adopt group-based assignment
and stable principal IDs while keeping the single-user local default simple.
[Unity Catalog best practices](https://docs.databricks.com/aws/en/data-governance/unity-catalog/best-practices).

Workspace bindings restrict which workspaces may access a catalog, independently
of a user's explicit data privileges. Our analogous check should intersect the
deployment's allowed data bindings with the effective principal's permissions.
A Supabricks workspace will be our execution scope; it is not automatically a
Databricks workspace or a compatible implementation of this API.
[Workspace-catalog binding](https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/workspace-catalog-binding).

## Lakebase: the closest end-to-end reference

Databricks also documents beta bundle support containing Lakebase projects, branches,
endpoints, PostgreSQL roles/databases and catalog resources. Its typical setup
connects a Lakebase-backed app to a service principal and binds the database
into Unity Catalog. This directly supports modeling our PostgreSQL resources,
analytical bindings and application assets in one resource graph, with explicit
references among them. It does not prove that our snapshot-based analytical
model has the same semantics as Databricks' catalog binding.
[Lakebase bundles](https://docs.databricks.com/aws/en/oltp/projects/manage-with-bundles),
[typical integrated project](https://docs.databricks.com/aws/en/oltp/projects/dabs-typical-project).

A Lakebase project contains branches, which in turn contain computes, roles and
databases. That is an infrastructure resource container. Our portable project
definition has a different lifetime: it can be installed into more than one such
runtime container. Preserve that distinction when choosing public names.
[Lakebase project structure](https://docs.databricks.com/aws/en/oltp/projects/manage-projects).

The Lakebase access tutorial explicitly distinguishes platform ACLs from
PostgreSQL role permissions and says they have no automatic synchronization.
For Supabricks, a cohesive user experience must resolve and explain both sets
of permissions while preserving their distinct enforcement authorities. A
unified screen must not suggest that granting project access grants SQL access.
[Two permission systems](https://docs.databricks.com/aws/en/oltp/projects/grant-user-access-tutorial).

## Snowflake: portable requirements, destination-owned access

Snowflake Native Apps declare references to consumer-owned tables, views,
secrets and other objects. The consumer binds those references to actual objects.
This avoids hard-coding the destination's object names into the application.
[References and requested privileges](https://docs.snowflake.com/en/developer-guide/native-apps/requesting-objects-privs).

A reference does not independently grant privileges: the documented reference
can become invalid when its creating role loses access. This is a useful model
for revocation. A deployment receipt should explain a binding, not function as
an everlasting authorization token.
[Consumer access and reference validity](https://docs.snowflake.com/en/developer-guide/native-apps/ui-consumer-granting-privs).

Application roles expose selected app capabilities to consumers. Supabricks
should similarly let a package name logical roles, while the receiving security
realm controls the assignment of actual users and groups. Package installation
must not import the publisher's administrator identities or credential store.
[Application roles](https://docs.snowflake.com/en/developer-guide/native-apps/creating-setup-script).

We should borrow these contracts, not require a marketplace, a cloud account or
Snowflake's execution model. External data bindings are especially relevant to
sharing the same Supabricks project across a laptop and an on-prem server.

## dbt: source, target credentials and resolved graphs

The dbt project references a separately configured connection profile. Profiles
hold credentials and named targets and can live outside the project. Supabricks
already has a useful precursor: `supabricks.toml` contains public identity while
credentials and worktree selections are private runtime state.
[dbt profiles](https://docs.getdbt.com/docs/local/profiles.yml).

The dbt manifest is a versioned generated artifact describing project resources
and their relationships. Supabricks should generate a resolved resource graph
for validation, planning, console display and provenance instead of making the
browser or notebook adapter interpret configuration independently.
[dbt manifest](https://docs.getdbt.com/reference/artifacts/manifest-json).

Adopt the separation and artifact contract. A dbt dependency, Jinja evaluator or
second package manager is unnecessary for our first project packaging slice.

## Open-source Unity Catalog and the Sail integration opportunity

The OSS Unity Catalog documentation describes external authentication plus a
local user/privilege database for authorization. Its quickstart uses Java 17.
This is a real service dependency and does not establish feature parity with
Databricks-hosted Unity Catalog. Qualify a pinned release and a named capability
set before selecting it for an installed Supabricks distribution.
[Authentication](https://docs.unitycatalog.io/server/auth/),
[users and privileges](https://docs.unitycatalog.io/server/users-privileges/),
[quickstart](https://docs.unitycatalog.io/quickstart/).

Sail **already includes a Unity Catalog provider** at our pinned 0.7.1 source
commit. Its configuration exposes a catalog URI, default catalog and credentials;
the implementation retrieves credentials and sends a bearer token. Its guide
also documents ambient environment-based credential discovery. This is a
promising integration starting point, not evidence that our packaged session
bootstrap currently supports governed UC access. Production adaptation must
supply explicit session-scoped configuration and prevent ambient credential
fallback.
[Pinned Sail guide](https://github.com/supabricks/sail/blob/9544c9253e981a82c5f9e493c43ce98a4d9d41b7/docs/guide/catalog/unity.md),
[pinned provider](https://github.com/supabricks/sail/blob/9544c9253e981a82c5f9e493c43ce98a4d9d41b7/crates/sail-catalog-unity/src/provider.rs).

The documented OSS Spark integration uses a JVM catalog plugin. Sail's Rust
provider is the candidate to test for Supabricks; installing that Spark plugin
into a Python notebook does not substitute for configuring our execution engine.
[OSS Spark integration](https://docs.unitycatalog.io/integrations/unity-catalog-spark/).

Keep OSS UC and Databricks-hosted UC as separate adapter profiles with separately
qualified authentication, APIs, storage access and policy behavior. Whether one
should become the default is deliberately unresolved. Until a provider is
qualified, our existing local metadata remains sufficient for local packaging.

## Standards and components to reuse

OCI permits non-container artifacts in its image manifest format using
appropriate media types. A later OCI transport can carry the same content-addressed
project artifact used for offline file exchange; running a container registry
need not become a prerequisite for `project pack`.
[OCI artifact guidance](https://github.com/opencontainers/image-spec/blob/main/artifacts-guidance.md).

Use an existing OIDC provider for future shared deployments. Keycloak documents
OIDC discovery, token, key, revocation and device authorization endpoints and is
a reasonable on-prem integration candidate. Provider/version selection and its
operational qualification belong in the IAM workstream; a default laptop
installation should require no identity server.
[Keycloak OIDC interfaces](https://www.keycloak.org/securing-apps/oidc-layers).

## Questions requiring experiments

1. Which OSS UC release, storage backend and identity flows can satisfy the
   pinned Sail provider without source changes? Measure startup, idle RSS, disk,
   backup/restore and offline behavior on both native targets.
2. Which hosted UC capabilities are accessible to an external Sail process, and
   how are temporary storage credentials, expiry and revocation enforced?
3. Can every analytical read path enforce the same resource and principal
   boundary, including direct file/Delta reads and cached sessions?
4. How should project-scoped logical PostgreSQL data export preserve extensions,
   types, constraints and sequences without copying users or physical identities?
5. Which execution isolation mechanism should shared on-prem notebooks use?
   Current host-user kernels cannot establish adversarial tenant isolation.

None of those experiments was run for this research. The existing local server
was left running; this change contains documentation only.
