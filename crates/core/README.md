# Portable core (P01)

`supabricks-core` owns compute-spec rendering, explicit compute paths, Ed25519
keys/JWKS/JWTs, legacy MD5 credential derivation, input validation, strict LSN
parsing and the branch ingestion decision. It also defines UUID resource IDs,
Neon tenant/timeline IDs, resource records, operation errors and capabilities.
Resource names are labels; renaming a branch does not change its identity or
engine timeline. These are Rust contracts, not a versioned public API or SQLite
schema. Persistence, migrations and resource revisions belong to P02.

`supabricks-local::plan_compute` consumes this core to produce a PG17 JSON spec
and an argv vector from explicit bundle/data/config paths and loopback ports.
It enables fsync, disables Unix sockets, and passes `--dev` to skip
compute_ctl VM-specific shutdown actions. Paths must be absolute UTF-8 paths;
spaces are preserved as part of each argument. It omits the POC fixture's fixed operation and cluster metadata. It neither
checks installed binaries nor launches them. The caller must supply verified component paths
and provision storage addresses and authentication before execution.

The existing operator is an adapter: Kubernetes Secrets, resources, statuses,
storage-controller HTTP calls and scheduling remain there. Its PG16 image
profile, compute command, rendered spec and MCP responses are preserved.
Malformed ingestion/flush LSNs now hold branching instead of comparing as zero.
The typed decision accepts a missing flush boundary and fails closed as well.

Run `just portable-check` (or its two commands in the root Justfile) for
portable contracts and the dependency boundary. CI runs them on Linux and
macOS without UI build or cluster setup. Workspace tests retain the operator's
MCP snapshots and chart contracts; its existing e2e/chaos/restore job checks
the deployment adapter. No local daemon, supervisor or CLI exists in P01.
