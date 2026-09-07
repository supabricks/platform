# Local analytical component probe

An isolated experiment supporting the
[preliminary Supabricks architecture](../../docs/plans/local-runtime-implementation.md).
It uses synthetic data, with no running Postgres, Kubernetes or cloud account.

## Reproduce

The maintained A00 environment and qualification command are documented in
[`python/analytics`](../../python/analytics/README.md). It locks CPython and all
transitive dependencies, runs this fixture in a supervised child process, and
qualifies native Linux x86_64 and macOS arm64 in CI.

`requirements.txt` and `result.json` below preserve the original investigation.
Use the new lock for current work; the historical report describes the original
fixture, not every check subsequently added by A00.

## What it proves

- A Sail server accepts ordinary Spark Connect SQL and DataFrame operations.
- Sail reads Delta tables written and subsequently updated/deleted/appended by
  delta-rs, including exact decimal values.
- Historical version reads work via the DataFrame reader.
- A catalog view over a named Delta table with explicit `VERSION AS OF` lets
  `spark.table("app.orders")` read a pinned version.
- An independent delta-rs reader agrees on the current table state.
- A new Sail engine can read current and historical table versions after the
  previous engine is stopped.

The JSON report records the installed versions, platform, checks and rough process
measurements. Startup timing begins **after Python imports**. RSS combines Sail,
the Python client, Arrow, and delta-rs in one process; it is not Sail's idle memory
or the footprint of a Supabricks installation. This is not a performance benchmark.

## What it does not prove

Postgres/Neon integration, transaction decoding, atomic publication across tables,
DDL mapping, branch export, crash-safe writes, abrupt process/power loss, native
macOS packaging, general Spark compatibility, worker isolation and the complete
installer remain untested. A snapshot probe is not evidence of a working OLTAP
platform.

## Exploratory findings

In Sail 0.7.1, `CREATE TABLE ... OPTIONS (versionAsOf '0') LOCATION ...`
still read the latest version in a separate probe. An explicit time-travel view
over a **named** source table worked. A view containing a direct `delta` path
relation was created successfully but failed when queried. The smoke test uses
the working named-table path; these alternative forms require upstream analysis
before use.

Unconstrained installation selected pandas 3, causing PySpark compatibility
warnings. The exercised environment pins pandas 2.3.3. Do not infer that these
pins have been certified across platforms.

## Recorded result

See [result.json](result.json) for the original September 5 investigation run.
The fixture was originally copied from the retired SSPC checkout. A00 extends
the script with additional decimal, snapshot-binding, cross-engine write and
process-exit checks. The original dependency pins and report are unchanged.
Current runs do not overwrite that historical observation.
