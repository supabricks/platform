# SY00 capture and boundary qualification

See the [contract and decisions](../../../docs/architecture/sy00-capture-probe.md).
This probe starts its own disposable native cell; it does not connect to the
user's active database. It tests real PG17/Neon logical capture, physical-branch
bootstrap, keyed replay and Delta version pinning. No production sync capability
is enabled by these files.

Use Python 3.12 and the existing native harness dependencies:

```sh
python -m pip install -r e2e/native/requirements.txt
python -m unittest discover -s e2e/native/sync -p 'test_*.py'
python e2e/native/sync/run.py --release /absolute/installed/release --report /tmp/sy00.json
python e2e/native/sync/qualify.py /tmp/sy00.json
```

`--release` names the directory containing `release.json`, `bin/`, `engine/`,
`helpers/` and `python/`. Its complete inventory is checked before execution.
The report records actual input hashes; an installed source build is not an
exact-archive claim. To reproduce CI's pinned archive inputs:

```sh
gh run download 35700396118 --repo supabricks/platform \
  --name release-linux-x86_64 --dir /tmp/sy00-archive
python e2e/native/sync/prepare.py \
  --archive /tmp/sy00-archive/supabricks-v0.1.0-alpha.35-linux-x86_64.tar.gz \
  --target linux-x86_64 --output /tmp/sy00-inputs
python e2e/native/sync/run.py --release /tmp/sy00-inputs/supabricks --report /tmp/sy00.json
python e2e/native/sync/qualify.py /tmp/sy00.json
```

Use `macos-arm64` for both artifact and target on Apple Silicon. The output
directory must not exist. `prepare.py` verifies the fixed archive hash, safe
extraction, target and every inventoried file. The receipt associates the archive
with the extracted manifest.

Only the report is shareable. Failed runs retain an owner-only `/tmp/sb-sy00-*`
fixture with private diagnostics; successful runs remove that fixture after
stopping and verifying owned processes. Never upload fixture state or daemon logs.

The decoder and reference model are deliberately bounded probe code. Their unit
tests verify rejection/atomicity, not production durability. `storage.py` runs in
the shipped analytical interpreter, using synthetic data passed through an
owner-only file. The SQL decoder transport, administrative identity and in-memory
checkpoint must not be promoted directly into a production capture service.
