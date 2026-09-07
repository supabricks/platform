# A00 analytical component environment

This is the developer qualification environment for the off-the-shelf Sail and
Delta stack. It is not yet included in the Supabricks installer. Postgres export,
atomic epochs and managed query sessions belong to A01–A03; private Python
redistribution and offline end-user packaging belong to R02.

## Reproduce

Use uv **0.11.21**, then run from the repository root:

```sh
uv sync --project python/analytics --locked --managed-python
python/analytics/.venv/bin/python python/analytics/qualify.py --output /tmp/analytical-qualification.json
```

Supported qualification targets are Linux x86_64 and macOS arm64. CI uses
Ubuntu 24.04 and macOS 15 native runners. CPython **3.12.13** and all 24 installed
packages are pinned. `uv.lock` records source/wheel URLs and SHA-256 hashes for
both targets; `requirements.lock` is its generated hashed export. The harness
rejects a different Python patch, missing/extra packages, version drift, or
package selections inconsistent with the component inventory.

`pyspark-client` is the small Spark Connect client, without a JVM. Its upstream
4.2.0 package is source-only, so this developer setup builds its Python wheel
with the locked setuptools, wheel and packaging versions. Build isolation is
disabled only for that package; native dependencies must use prebuilt wheels.
R02 must prebuild this wheel and ship the complete environment so users do not
need uv, pip or build tools. The managed Python distribution is a qualification
input, not the selected relocatable product runtime.

To regenerate the lock/export after an intentional dependency change:

```sh
uv lock --project python/analytics
uv export --project python/analytics --locked --format requirements-txt --no-emit-project --output-file python/analytics/requirements.lock
```

Requalify **both** targets. CI verifies the export is current and keeps the
existing required `analytical-probe` check as an aggregate that fails if either
native job fails or is cancelled. The original investigation's
[`requirements.txt`](../../spikes/local-analytics/requirements.txt) and
[`result.json`](../../spikes/local-analytics/result.json) remain historical inputs.

## Contract and limitations

The [fixture](../../spikes/local-analytics/smoke.py) uses real Spark Connect,
Sail, Arrow and delta-rs with synthetic local tables. It checks:

- SQL/DataFrame reads and decimal aggregation, including values beyond double's
  exact integer range, fractional values, negatives, nulls and empty tables.
- delta-rs append/update/delete observed by Sail, and Sail writes/deletes read
  and subsequently mutated by delta-rs and reread by Sail.
- Historical DataFrame reads; two named `VERSION AS OF` views that retain
  different versions after later writes; rebuilding the historical binding
  after stopping and starting the Sail engine.
- A separate delta-rs reader process returning correct data **and exiting
  successfully**. A native local Arrow filesystem is used, while delta-rs
  still resolves the active files and schema from the transaction log.
- Known unsafe binding forms and unsupported commands, with narrowly matched
  expected behavior. An unexpected change fails qualification for investigation.

The snapshot mechanism for A03 is a view over a **named Delta source table**
with explicit `VERSION AS OF`. On Sail 0.7.1, a table's `OPTIONS (versionAsOf '0')`
is ignored for reads, while a view over a direct `delta` path is accepted at
creation and fails at readback with an identifier parse error. Neither is a
qualified substitute for the named-table form. Sail SQL `UPDATE` is unsupported;
the fixture verifies rejection leaves data unchanged. delta-rs provides updates.

A short-lived reader using the default delta-rs Python-backed Arrow filesystem
aborted at interpreter shutdown during A00 investigation. `read_delta.py` uses
Arrow's native `SubTreeFileSystem(LocalFileSystem)` for **local files only** to
avoid that path. No sleeps or forced successful exits hide reader failures.
Remote filesystems and general threaded reader shutdown are not qualified here.
See the [delta-rs filesystem API](https://delta-io.github.io/delta-rs/api/delta_table/)
for custom filesystem semantics; the observation does not establish the root
cause of every upstream shutdown report.

## Managed sessions

[A03 analytical sessions](../../docs/architecture/a03-analytical-sessions.md) use
`session.py` and `shell.py` alongside the configured A01 exporter. The A03 native
suite found lossy decimal min/max statistics in delta-rs 1.6.3 that the earlier
multi-row A00 fixture did not expose. New exports disable column statistics;
older decimal epochs with those statistics require a refresh before Sail access.

## Evidence and measurement boundaries

Recorded passing reports for both native targets are in [`evidence/`](evidence/README.md).
Each CI target uploads `analytical-qualification.json` and its process log.
The JSON includes target/OS, Python build, all package versions, Git revision and
working-tree state, hashes of the fixture/environment inputs, individual checks,
worker exit status and measurements. The parent has a 180-second deadline and
kills the fixture process group on timeout; failures still produce a report.
Python warnings are errors in the fixture and independent reader, so the earlier
pandas compatibility warning cannot silently return.

Startup has two boundaries: process launch to first SQL result (including
interpreter startup, imports and tiny fixture setup), and server start to first
SQL result (excluding those costs). RSS is sampled every 10 ms over the fixture
process and descendants, including Python, Spark client, Sail, Arrow and the
writer/independent reader. The harness process is excluded. Shared pages may be
counted multiple times, transient peaks may be missed, and JVM absence is only
observed at sampling points. The fixture also records combined main-process RSS
after the workload. These are synthetic observations on the recorded host, not
isolated Sail memory, cold-cache benchmarks or product capacity claims.

No Postgres consistency, multi-table publication, export throughput, power-loss
recovery, generic Spark compatibility or native release packaging is established
by this fixture. The earlier Postgres POC engine is still unidentified; the
retained Sail probe is an independent analytical experiment.
