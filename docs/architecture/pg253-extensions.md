# PG253: installed PostgreSQL extension catalogue

Issue [#253](https://github.com/supabricks/platform/issues/253) adds a local
operator contract for the console's Extensions tab. The
[operator guide](../handbook/postgres-extensions.md) describes requests,
ownership, analytical type limits and restart behavior.

The engine bundles `pg_stat_statements`, `pgcrypto`, `pg_trgm`, `uuid-ossp` and
`citext`. Vector and PostGIS remain unavailable pending separate dependency and
runtime qualification. Listing joins the running database's installed extensions
with its available extension control files, so older installations and installed
extensions whose files are missing remain distinguishable. Extension state stays
in PostgreSQL; there is no second control-plane catalogue to reconcile.

The API, console workspace command, CLI and MCP tool share one implementation.
Project and branch revision checks precede private SQL access. It requires a
running, reconciled compute and refuses governed branches. Mutations also refuse
active capture and enabled event triggers. Only the five reviewed identifiers
reach DDL. The managed schema has private administrator ownership and no
application CREATE privilege; existing extensions elsewhere are never adopted.
Removal uses RESTRICT and preserves dependent data.

`pg_stat_statements` is preloaded only when its control file is present. Utility
tracking is off to avoid retaining private credential DDL. A new default-false
Neon compute option suppresses upstream's automatic extension creation; platform
computes opt in so explicit disable survives restart. Disabling SQL objects does
not unload the preloaded library or stop internal collection. This is a
prerequisite for #248, not a query activity or percentile implementation.

## Qualification evidence

The retained [local receipts](pg253-evidence/local.json) identify source and
binary hashes, test counts, commands and limitations. The
[platform fixture](pg253-evidence/extensions.json) exercises actual native
functions, unsafe ownership, event triggers, rejected names, revision fencing,
dependent tables, physical branch inheritance and isolation, suspension,
daemon restart, repeated desired-state requests, project isolation and CLI
access. The [legacy fixture](pg253-evidence/legacy.json) checks honest absence
and unchanged preload behavior with the previous engine.

The [engine smoke](pg253-evidence/engine-smoke.json) additionally checks relocated
paths, exact decimals, explicit-LSN branching, restart after abrupt compute
termination, GiST/cache behavior and lazy SLRU download. It uses LocalFs remote
storage; it is not a whole-cell power-loss or S3 qualification. The platform
fixture uses its normal local SeaweedFS storage cell.

The selected archive identities and target-specific CI qualification live in
[`native-linux.json`](../../components/provenance/native-linux.json) and
[`native-macos.json`](../../components/provenance/native-macos.json).
Previous receipts are retained alongside this record when those pins change.
Only archives whose clean source identity, PostgreSQL regression result,
extension inventory and relocated runtime smoke all match are promoted.
The Linux CI probe also runs without external networking or build tools.
macOS qualification runs on a build-capable runner; clean-host distribution,
notarization and release signing remain separate gates.

## Failed attempts and boundaries

- Host execution first hit the existing file-watch quota/startup failure
  tracked in [#221](https://github.com/supabricks/platform/issues/221), before
  extension checks. Qualification moved into a disposable, unprivileged
  UID 1001 container with networking disabled and read-only build inputs.
  No other workload was stopped and no host limit was changed.
- The first bundle verifier incorrectly required a base SQL file at the
  default version. PostgreSQL supports a base file plus upgrade scripts;
  the verifier now checks the reachable upgrade graph. Its regression test
  rejects a broken chain and accepts a complete chain.
- The first macOS CI build used the BSD UUID configure option. PG's Darwin
  implementation uses the e2fs-style API; both targets now select that API.
- A platform fixture attempt reached SQL readiness before control-plane
  reconciliation after restart. The API correctly refused the premature
  request. The fixture now waits for catalogue readiness before continuing.
  A later fixture assertion also needed the exact `not_found` error code;
  the cross-project request itself was correctly refused in both attempts.

The inherited engine-fork workflows require private upstream infrastructure;
their startup/label failures are tracked in
[#341](https://github.com/supabricks/platform/issues/341), independently of the
native-engine qualification. Governed extension lifecycle needs additional
routine/privilege qualification and remains refused, tracked in
[#342](https://github.com/supabricks/platform/issues/342).

No sync throughput, query latency, vector/PostGIS support or new analytical type
support is claimed. The SP workstream and SF100 load were not resumed.
