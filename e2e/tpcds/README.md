# TPC-DS input and compatibility harness

See the [EQ plan](../../docs/plans/tpcds-end-to-end-qualification.md) and
[EQ00 results](../../docs/architecture/tpcds-eq00.md). These tools do not claim
end-to-end qualification or an official benchmark score.

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
