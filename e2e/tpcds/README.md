# TPC-DS input and compatibility harness

See the [EQ plan](../../docs/plans/tpcds-end-to-end-qualification.md) and
[EQ00 results](../../docs/architecture/tpcds-eq00.md). These tools do not claim
end-to-end qualification or an official benchmark score.

## EQ02 native SF1 workflow

See [the live assessment and failed attempts](../../docs/architecture/eq02-sf1.md).
`load.py` verifies the existing generated files, starts a fresh installed cell,
enrolls all original tables with continuous sync, then performs bounded COPY.
It requires 80 GiB free on its state filesystem; the cell's 64 GiB ceiling is
sampled, and the original input/installed release must be budgeted separately.
Run under the existing descendant supervisor in an isolated 8-CPU/16-GiB
container with swap/network disabled and read-only input/release mounts:

```sh
python3 install/native/catalog_gate.py --timeout 7500 --report /reports/load.cleanup.json -- \
  python3 e2e/tpcds/load.py --release /release --inputs /inputs \
    --dataset /sf1-generation --output /reports/load --max-unpublished-rows 65536
```

The dataset directory includes `generation.json` and `data/`. Omitting the row
window reproduces unrestricted ingestion; retained attempts show it exceeding the
current WAL envelope. The window is specific to this one-loader, insert-only
fixture. It waits outside transactions and reports all waiting time. The
`load-profile.json` contract explicitly decodes the pinned generator's Latin-1
country names; bytes/checksums stay unchanged. Each attempt needs a fresh output
directory. No failed or ambiguous batch is automatically retried; inspect its
attempt/ack ledger and private state instead. A load PASS is only a committed and
drained boundary, not full table or query correctness.

After a successful stopped load, retain the same writable state mount/path and
installation for `verify.py`. It restarts that private cell, checks every source
row against the pinned Delta versions, then executes all 103 original queries
through managed product sessions. Credentials travel only over the child worker's
stdin. The product's normal session resource limits stay in force.

```sh
python3 install/native/catalog_gate.py --timeout 18000 --report /reports/verify.cleanup.json -- \
  python3 e2e/tpcds/verify.py --release /release --inputs /inputs \
    --load /reports/load --output /reports/product
```

Run `reference.py` **separately** with the hash-pinned Spark 4.2.0 environment from
`reference-requirements.txt`, the captured bundled Java 17 runtime and its own 8-CPU/16-GiB resource boundary.
It uses an 8 GiB JVM driver, native logical types including CHAR table semantics,
the identical input files and encoding profile, and all statements without test
exclusions. Reference rows, schemas and query plans remain in its fresh output.

```sh
python e2e/tpcds/reference.py --inputs /inputs --dataset /sf1-generation --output /reports/reference
python3 e2e/tpcds/compare.py --product /reports/product --reference /reports/reference --output /reports/comparison.json
```

Full results have a declared 16 MiB evidence ceiling and queries a 120-second
execution ceiling. Exceeding either is an explicit non-passing result, never a
passing truncated preview. Comparisons use exact positional SQL types/values.
Ordering differences and LIMIT boundary ties require explicit review; floating
point differences are not silently rounded. The reference is Apache Spark JVM;
the installed product engine is Sail through Spark Connect. Their measurements
must stay separately labeled. Both full suites have now run. The
[SF1 results](../../docs/architecture/eq02-sf1-results.md) pass the full load and
exact table comparison; 69/103 product statements are correct after explicit
ordering review, with 34 tracked failures/mismatches. Full analytical
qualification is still incomplete.

## Pin and inspect inputs

Python 3.11+ is required. From the repository root:

```sh
python3 -m unittest discover -s e2e/tpcds -v
mkdir -p build/tpcds
python3 e2e/tpcds/inputs.py --fetch --inputs build/tpcds/inputs --report build/tpcds/inventory.json
```

Every archive and selected file is hash-checked against `inputs.lock.json`.
Destinations and reports must be fresh; failed downloads are preserved, not
overwritten. Upstream licenses/notices are retained with selected inputs. The
inventory includes 24 business tables plus separate generator metadata and all
103 SQL statements / 99 templates, without Spark's test exclusions. This does
not execute any SQL. `--fetch` can be omitted to verify existing selected inputs.

## Build the generator

Use Linux GCC, make, flex and bison. Clone the pinned source:

```sh
git clone https://github.com/databricks/tpcds-kit build/tpcds/kit
git -C build/tpcds/kit checkout --detach 1b7fb7529edae091684201fab142d956d6afd881
make -C build/tpcds/kit/tools OS=LINUX CC='gcc -fcommon' YACC='bison -y' LEX=flex -j1
```

The captured pilot used the host's GCC 13 and locally extracted Ubuntu bison/flex
packages, with `BISON_PKGDATADIR` pointing at the extracted bison share directory.
Source was unchanged. Build dependencies, binary/distribution hashes, compiler
version and original command are in the evidence provenance. Exact binary hashes
can differ across build toolchains; each run records its actual binaries.

## Bounded SF1 generation

Choose a fresh output directory on a disk with sufficient space. The complete
`data` directory path must be shorter than 80 bytes due to the upstream tool's
parameter limit. This example uses `/data2`; adapt the storage root explicitly.

```sh
python3 e2e/tpcds/generate.py --kit build/tpcds/kit --inputs build/tpcds/inputs --output /data2/supabricks-eq/sf1-01
```

The lock fixes SF1, seed, one child and Linux resource bounds. The harness retains
stdout/stderr, success/failure, timing, binary hashes, file checksums, actual row
counts and empty-field counts. It checks complete rows and required values, not
referential integrity or analytical results. The dataset metadata includes run
timestamps; compare repeatability on the 24 business files, not that timestamp.
The disk ceiling is sampled, not a filesystem quota. Do not use this SF1 harness
to admit larger scales without a separate whole-stack capacity budget.

## Installed capture-admission probe

Use a verified installation, an isolated qualification environment with
`e2e/native/requirements.txt` plus psutil, and the existing descendant supervisor:

```sh
python3 install/native/catalog_gate.py --timeout 600 --report /reports/cleanup.json -- \
  python3 e2e/tpcds/compatibility.py --release /release \
    --inputs /inputs --report /reports/compatibility.json
```

The probe starts a private native installation, creates/drops only its own
disposable tables, invokes the installed capture inspector in a separate worker,
checks SQL lexical admission, and shuts down. Credentials travel through the
worker's stdin and are not included in reports. The supervisor accounts for
descendants on failures as well as success. Run it inside a private container
with no network, a read-only installation/source mount, writable reports and a
declared CPU/memory budget. The EQ00 probe used 4–7 CPU affinity and 8 GiB memory
with swap disabled. It records an engineering-overlay scope; using a full archive
later requires an explicit exact-archive provenance record, not relabeling this
receipt. SQL acceptance is lexical only: no query or sync operation is executed.

Do not replace rejected native columns or composite keys to report a passing
TPC-DS run. Address tracked compatibility issues first, then load with sync active
and verify full product/reference results.

## EQ01 composite-key qualification

`composite.py` runs the seven native TPC-DS composite schemas plus populated
two-/three-key fixtures through installed continuous sync and Sail. It checks
shared key prefixes, reordered index keys, INCLUDE payload, key moves, deletes,
unchanged TOAST, exact decimals, capture SIGKILL and daemon restart. The TPC-DS
tables here are empty schema probes, not a full dataset/query qualification.

```sh
python3 install/native/catalog_gate.py --timeout 600 --report /reports/cleanup.json -- \
  python3 e2e/tpcds/composite.py --release /release --report /reports/composite.json
```

Omitting `--inputs` uses the hash-verified committed EQ00 inventory for offline
release CI. The same checks are mandatory in the `composite` installed sync suite.
`--control-only` runs an unchanged pair of 10,000-row single-key tables with twelve
64-row transactions, reporting commit and acknowledgment-to-observed-publication
latencies and final exact equality. Use fresh installations, alternate baseline/
candidate order and keep raw repetitions. The observer polls at 200 ms; this is a
short EQ regression screen, not sustained SP or TPC-DS performance qualification.

For a local engineering candidate, `package.py` verifies the base payload and
creates an unsigned Python-only overlay. It breaks hardlinks before replacing
sources/checked-hash bytecode and records every changed hash. For #170:

```sh
python3 e2e/tpcds/package.py --base /baseline --destination /candidate --repo . \
  --proof /reports/package.json --source capture/source.py \
  --source incremental/rows.py --source incremental_worker.py
```

Production release qualification must use the unchanged built archive through
`install/native/qualify_sync.py`, including Linux/macOS offline installation and
the composite suite. An engineering overlay does not substitute for that gate.

## EQ01 DATE/CHAR qualification

See [the contract and SQL dialect differences](../../docs/architecture/eq01-date-char.md).
Build the pinned Sail artifact with `components/build-sail.py` before packaging
CHAR. DATE-only source is isolated in commit `f3c4f11` (four worker files plus
DATE regression tests); use that revision when reproducing the DATE overlay.
`package.py --sail-artifact DIRECTORY` verifies that source-built artifact
and copies its wheel/provenance into an unsigned engineering overlay, checking
all base file hashes and installation verification. It unlinks replaced hardlinks
before writing. DATE-only overlays omit this argument and `export.py`, retaining
the separate DATE slice sources. Preserve those immutable artifacts for controls.

Run each typed fixture in a private installed environment under the descendant
supervisor (the standard `installed_sync.py --suite date` / `--suite char`
adapters also make them mandatory release gates):

```sh
python3 install/native/catalog_gate.py --timeout 600 --report /reports/cleanup.json -- \
  python3 e2e/tpcds/sync_types.py --release /release --kind char --report /reports/result.json
```

Each result is compared with committed Apache Spark JVM goldens before a check
passes. To reproduce those goldens, use a separate Python 3.12 environment with
`uv pip install --require-hashes -r e2e/tpcds/reference-requirements.txt`, the captured bundled Java 17 runtime,
and `type_reference.py --report FRESH_PATH`. The fixture creates typed Parquet
reference tables; it does not use Sail or reuse product query results as expected
values. Retain failed attempts and compare all rows/types, not generated column
names, which legitimately vary between engines.

After builds/correctness tests finish, run the nine sequential unchanged scalar
controls with `type_controls.py --baseline BASE --date DATE_ONLY --char DATE_CHAR
--output FRESH_DIRECTORY --image sha256:IMAGE_DIGEST`. All releases must be under
the repository for its read-only Docker mount. The fixed order rotates all three
arms over three repetitions; each cell has 8 CPUs, 16 GiB, no swap/network, two
10,000-row tables and twelve 64-row transactions. There are no quiet-period sleeps
or automatic replacements of failures. Raw commands, results, cleanup and timing
samples are retained. Treat the summary as descriptive compatibility evidence,
not a sustained throughput benchmark or an SP qualification.

## Bounded post-compaction merge regression (#184)

The mandatory installed `merge` suite uses 1,888,080 mixed-width rows in one
compacted file and a separate 131,072-row small-file layout. Both receive 16,384
inserts and sparse updates/deletes/key movement. The old runtime hangs on the
compacted fixture; a single-partition-only correction exhausts the bounded hash
build pool on the small-file fixture. The complete patch must pass both.
It verifies every field and key exactly at all three versions, including replay
of a saved plan after a commit-before-receipt fault. Each generation/apply/read
phase runs in a separate installed Python process with a 90-second external
timeout; reported apply RSS excludes fixture generation and equality checking.
This fixture is synthetic storage regression coverage, not a PostgreSQL/Sail
throughput result or a replacement for the full SF1 run.

```sh
python3 install/native/catalog_gate.py --timeout 600 --report /reports/cleanup.json -- \
  python3 e2e/native/installed_sync.py --release /release --suite merge --report /reports/result.json
```

`package.py --delta-artifact DIRECTORY` accepts only a verified artifact from
`components/build-deltalake.py`, replacing the native wheel and recording its
source, patch, lock and dependency notice inventory in an unsigned engineering
overlay. Production qualification still requires both unchanged native archives.

The Linux external apply-memory observer is maintained in `apply_memory.py`.
Invoke it as `python3 e2e/tpcds/apply_memory.py OUTPUT SUPERVISOR_COMMAND...`
with an existing private output directory and the usual descendant supervisor.
It requires psutil from the native qualification environment. Negative process
roles are rechecked after fork/exec; confirmed workers are tracked by PID and
birth time. Sampling can still miss short-lived workers or their final peaks.
The earlier build-local observer could cache a pre-exec negative role for an
entire worker lifetime (#191); retain that limitation on its historical samples.

## Installed compaction capacity regression

The `capacity` suite exercises a >512 MiB wide-text live table, compaction plus
new-key append under the original 1 GiB generation / 768 MiB worker limits,
commit-before-receipt recovery, exact old/new values, and a 512-version append
history. The installed source-built Delta wheel supplies bounded write settings;
the upstream PyPI wheel does not implement that API. This synthetic suite is a
required archive gate, not a PostgreSQL throughput measurement.

## Completed SF1 baseline and retained-state verification

[The full SF1 results](../../docs/architecture/eq02-sf1-results.md) retain the
successful 19,557,335-row load, exact 24-table comparison, and all 103 query
attempts. Sixty-nine statements are correct after explicit order review; 34 have
tracked failures/mismatches in that baseline. The [decimal AVG candidate](../../docs/architecture/eq02-decimal-avg.md) raises correctness to 78/103 on retained SF1, with 25 remaining issues. Larger-scale qualification remains open.

`verify.py` defaults to the original load release. After a supported stopped
**native-only** installation upgrade, pass `--load-release ORIGINAL_RELEASE`
alongside `--release CANDIDATE_RELEASE`. The verifier requires the original
receipt identity, identical manifest content except version/native binary, a
completed upgrade receipt with backup, and matching runtime identity. It records
both releases; it never rewrites the load result. A copied worktree must be
explicitly attached to its existing deployment through `project attach` before
its project API calls can succeed. Retained query-only experiments do not need
another SF1 load.


### Qualifying a Sail correction without reloading SF1

Build a clean, reviewed Sail commit with `components/build-sail.py` and the
updated source lock. `package_sail_candidate.py` creates an unsigned engineering
package from a verified existing release, replacing only the same-version Sail
wheel and its source/build provenance. It checks all installed artifact bytes
and preserves every unrelated payload. New packaged tests must also come from
the verified wheel. The base package is unchanged.

Apply that candidate to a **copy of the stopped successful cell** using
`supabricks installation upgrade`, with an explicit stopped backup and original
release. Then run `verify.py --load-release ORIGINAL_RELEASE --sail-artifact
ARTIFACT_DIRECTORY` with the candidate release. The verifier checks the original
load identity, exact artifact-bound payload changes, completed upgrade journal,
backup identity, and applied runtime identity. It still verifies all 24 tables,
the original publication epoch, and all 103 original SQL statements. The source
load receipt and independent Spark reference are never rewritten.

A copied worktree can explicitly attach to its existing deployment using
`--attach-deployment DEPLOYMENT_ID`. This invokes the supported project attach
command, records its receipt, and verifies the original runtime project before
waiting for sync health. It does not create a replacement dataset or deployment.

This path qualifies an analytical correction against retained data; it does not
claim a signed release or broad binary-upgrade compatibility. Keep baseline and
candidate results in separate fresh output directories and preserve both exact
comparison ledgers, timings, and descendant-cleanup reports.
