# Native cell and branch qualification (P03/P04)

This suite runs the real Rust daemon, authenticated Process Compose, one broker,
one safekeeper, a directly attached pageserver, native PG17.8 computes and
SeaweedFS. No Kubernetes, Docker, controller, controller database or LocalFs
remote backend is involved.

Build the portable binary, prepare the helpers, then use an E01-verified bundle:

```sh
cargo build --locked -p supabricks-local
python3 -m pip install -r components/requirements.txt -r e2e/native/requirements.txt
python3 components/prepare-native-cell.py linux-x86_64 /tmp/sb-helpers
python3 components/verify-native-bundle.py /path/to/engine
./target/debug/supabricks up --data-dir /tmp/sb-cell --bundle /path/to/engine --helpers /tmp/sb-helpers
./target/debug/supabricks status --data-dir /tmp/sb-cell
./target/debug/supabricks down --data-dir /tmp/sb-cell
```

On Apple Silicon use `macos-arm64`. Preparing its SQLite-enabled SeaweedFS
requires the Go version in `components/native-cell.lock.json`; the installed
runtime does not require Go. This engineering setup consumes qualified CI engine
archives. They expire; a signed, stable release channel belongs to R01.

Run the complete isolated qualification:

```sh
python3 e2e/native/cell.py --binary target/debug/supabricks \
  --bundle /path/to/engine --helpers /tmp/sb-helpers --report native-cell.json
```

The test creates a fresh `/tmp/sb-p03-*` root and copies the executable there so
concurrent development builds cannot replace a running daemon's executable.
It tests SQL credentials, S3 upload/head/range/list/multipart/delete, dynamic
compute lifecycle without storage restarts, abrupt compute_ctl/supervisor
and daemon loss, and cold restore after deleting only disposable engine state.
Object data and SQLite control state are retained. Failure roots stay available
for inspection. Private configurations and service logs contain credentials;
CI uploads only qualification reports and binary provenance.

Linux CI additionally passes `--disk-full`. This mounts a **384 MiB disposable
tmpfs** at the isolated test root's object directory, fills that mount until an
actual `ENOSPC`, and requires S3 writes to fail. It frees the test filler, restarts,
and verifies previously acknowledged SQL and object data. It never fills the
host filesystem. This option requires noninteractive sudo for mount/unmount.

SIGKILL, disk-full and restart tests do not establish power-loss safety. In
particular, directory-entry persistence, volume-index repair and delete/compaction
ordering still need a dedicated filesystem fault qualification before a public
durability claim. See [the architecture notes](../../docs/architecture/native-cell.md).

Run P04 branch qualification with the same prepared engine and helpers:

```sh
python3 e2e/native/branches.py --binary target/debug/supabricks \
  --bundle /path/to/engine --helpers /tmp/sb-helpers --report native-branches.json
```

This uses a separate `/tmp/sb-p04-*` root. It checks concurrent duplicate creation,
application/control credential separation, immediate-write head branching,
parent/child isolation, branch-of-branch, suspended-parent wake/restore, explicit
LSN and RFC3339 time, interrupted creation at a persisted LSN, ingestion outage,
invalid history, default/parent protection, TTL admission and SQL/lease draining,
interrupted deletion, S3 reclamation, runtime configuration migration, and refusal
to invent control metadata over surviving engine files. Both suites run on Linux
x86_64 and macOS arm64 in `native-cell.yml`. Failure artifacts redact private
credentials; raw roots remain private. These tests do not establish power-loss
safety or a supported backup product.

See [the branch API and recovery contract](../../docs/architecture/native-branches.md).
