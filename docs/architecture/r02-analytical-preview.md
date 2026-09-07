# R02: complete local analytical preview

The default localhost distribution includes Postgres, the local storage cell,
CPython 3.12.13, and the locked Sail/Delta/Arrow/Spark Connect environment.
`supabricks up` discovers both the engine and analytical workers relative to the
installed executable. No `analytics configure`, uv, pip, system Python, JVM or
first-query dependency download is needed. `--postgres-only` explicitly produces
the smaller R01 profile. Public hosting remains deferred.

## Packaging contract

`components/analytical-runtime.lock.json` pins the Python Build Standalone
20260610 install-only archives by SHA-256 for Linux x86_64 and macOS arm64. The
existing `python/analytics/uv.lock` and hashed requirements retain the qualified
package versions. Assembly requires wheels for every package except the
source-only Spark Connect client, whose hashed source is built on the builder
with the locked setuptools/wheel/packaging tools and no build-time resolution.
The installed environment has no pip bootstrap; package code, wheel hashes,
source identities and available redistribution notices are inventoried.

Python uses its relocatable base installation directly. Internal archive links
are flattened to regular files for the existing signed installer. Private launch
wrappers ignore Python environment overrides and user site packages and disable
bytecode writes. This preserves immutable release verification after queries and
relocation. Raw same-user Python and UDF execution retain the A03 trust model.

Linux assembly checks every analytical ELF object, preserves wheel-relative
loader paths, adds private runtime/engine library paths and bundles additional
non-glibc system dependencies with their package identity and copyright notices.
macOS checks Mach-O dependencies for builder paths. Separate offline qualification
jobs exercise the relocated archives with external networking denied; macOS also
denies Homebrew libraries. Publisher signing, downloaded-file notarization,
complete redistribution audit and upgrades remain R03/public-release gates.

The analytical profile requires its worker files in the release manifest. A
source build or explicit Postgres-only profile can still configure a developer
worker. The complete profile always uses its own worker, including after a
stopped installation is relocated. It rejects custom configuration rather than
silently accepting settings it would ignore. Existing R01 data-root identity
checks still reject switching release versions; R02 does not introduce upgrades.

## Qualification

`install/native/qualify.py --benchmarks` drives the real signed curl-to-Bash
installer, application writes, automatic first snapshot, pinned-session refresh,
SQL and ordinary Spark DataFrames, branch migration and its analytical snapshot,
MCP discovery, suspension/wake, offline restart and relocation. It verifies that
analytical use does not mutate the signed file inventory. Both targets run from
the exact archives in the native-release workflow.

The benchmark uses a separate disposable database and 10 MB, 100 MB and 1 GB
payload targets (decimal units). Each row contains 1,024 bytes formed from 32
distinct deterministic MD5 hex strings; this has moderate compressibility and is
not a representative application workload. Reported source bytes exclude row,
index and WAL overhead. Each full refresh is followed by a count/length query,
then retention cleanup. Reports contain load time, export throughput, disk bytes,
installed/download size, cold/warm query and runtime readiness, and wake latency.

A 200 ms sampler observes the installation daemon and its live descendants,
including storage, computes and analytics. It records the actual OS, architecture,
CPU count, host RAM, RSS and CPU consumption. RSS may double-count shared pages
and omit short-lived peaks. Idle CPU is measured over five seconds. Hosted CI
machine measurements must not be described as measurements on a named 16 GiB
laptop; hardware-specific qualification remains explicit in the evidence.

Qualification results and an initial measured ceiling will be recorded after
both exact archives pass. No capacity or public release readiness is inferred
from successful assembly alone.
