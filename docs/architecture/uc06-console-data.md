# UC06: project Data browser

The standalone console exposes owned live PostgreSQL tables, analytical snapshots,
owned catalog publications and explicit dataset bindings in a project-scoped Data
view. Other projects' publication summaries appear only after **Add existing
dataset**. Discovery grants no storage access or destination binding. Descriptors
show provider, owner, branch, schema, fixed revision and observation time. Live
PostgreSQL table/column comments are bounded metadata; absent snapshot comments
stay unknown. Row freshness relative to the live database is unknown.

The publication flow refreshes an analytical snapshot, ensures the owned catalog
namespace, previews the complete publication and submits the reviewed identity.
Withdrawal requires confirmation and displays durable retention holders. Pending
publication and apply requests retain their exact idempotent arguments in the
browser session; reload recovers status and a lost reply offers an explicit retry.
No query, notebook cell, publication or apply is replayed automatically.

Adding/removing a dataset edits the public root manifest through a shared
`project dataset-draft` / MCP `project_dataset_draft` contract. The edit requires
the expected manifest hash, preserves TOML comments, rejects links and validates
the complete source graph before an atomic replacement. Included-fragment
requirements remain editable in their source file; the browser will not move or
silently shadow them. A draft changes source only. The existing reviewed
plan/apply performs every destination mutation, including removal. Discarding a
plan leaves its source declaration available for further editing or packaging.

Updates show the prior/new schemas and provenance in the plan. Each new
catalog-mode analytical session resolves the current fixed project bindings.
Existing readers retain their original inputs. A handoff from an owned publication
also carries its exact epoch; it never silently follows the current head. A
notebook handoff writes a new project-owned notebook and opens it only by explicit
selection, without executing cells or starting a kernel. Session and notebook
views show the resolved data identities and available environment identity.

Browser publication/health responses omit internal storage URIs, service endpoints
and raw provider diagnostics. CLI/MCP retain their operator contracts. Existing
cookie, Origin, CSRF, daemon-generation and project binding checks protect all new
commands. Source editing and discovery are shared contracts, not browser-specific
authority. The profile remains single-owner local execution; this is not IAM.

Observed provenance connects import receipt/source checksum to a PostgreSQL table,
snapshot epoch to publication, publication to installed binding and execution to
its recorded data/environment inputs. The console does not infer arbitrary
Python, column-level lineage or a common source transaction across datasets.

The native browser fixture stages the current console, platform binary and pinned
source-built UC over the qualified PG/Sail baseline. It is a component workflow
qualification, not a claim that the entire new archive passed release gates.
Existing console, notebook, ingestion and package browser suites remain mandatory.
