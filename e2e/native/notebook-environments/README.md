# NE01: service/kernel separation probe

This is a builder and qualification harness, not an environment manager. It
derives an isolated installation from the PR34 qualified native archives
(`native-release` run `34486403948`). It verifies the archive checksum and complete
baseline inventory before copying anything. Only the copied analytical Python
wrapper changes; a re-sealed derivative manifest identifies that change.

The fixture writes `.ne01-python` in each private test project. The derivative
wrapper reads that selection **only for the fixed notebook kernel entry point**.
The existing Rust daemon, A03 admission, `supabricks child` ownership gate, private
Jupyter Server, bounded Session and Spark bootstrap run unchanged. Every service
worker continues to use bundled service Python. Never ship this wrapper or use
this fixture file as the production environment-selection contract.

`assemble.py` traverses the existing notebook lock from `ipykernel` and
`pyspark-client`, evaluating platform markers. Every registry wheel retains its
locked input hash. Spark Connect's existing source-only distribution is built on
the builder using the qualified service's setuptools/wheel/packaging and pinned
uv, offline and without build isolation. The report records its source and
resulting wheel hashes. `fixtures.lock.json` adds two humanize versions and the
native xxhash wheel. Wheel license metadata stays in the artifacts/installed
distributions; uv's pinned upstream license texts are included separately.

The derivative is relocated before any venv is built. `run.py` starts with an
empty private HOME/cache and a PATH containing no tools, invokes bundled Python
and uv by absolute path, and creates venvs at their final paths. Only the
cancellation fixture contacts a controlled loopback HTTP index. CI denies all
external network access for the entire runtime qualification.

Run on the matching native target (builder access to pinned upstream artifacts
is required):

```bash
gh run download 34486403948 -n release-linux-x86_64 -D build/ne01-inputs
python3 e2e/native/notebook-environments/prepare.py \
  --directory build/ne01-inputs --output build/ne01-package --target linux-x86_64
"build/ne01-package/relocated probe/service/python/runtime/bin/python3.12" -I -B \
  e2e/native/notebook-environments/run.py \
  --probe "build/ne01-package/relocated probe" --target linux-x86_64 \
  --report build/ne01-reports/qualification.json
```

Use the workflow's network isolation wrapper for retained offline evidence.
Reports contain hashes, timings, byte counts and checks, not private launch
tokens or connection files. Failed runs preserve their private `/tmp/sb-ne01-*`
workspace for local diagnosis; CI uploads only the public report allowlist.
Successful runs stop the daemon and remove their private workspace.

See the [NE implementation plan](../../../docs/plans/notebook-environments-implementation.md)
for the decision gate and later runtime integration slices.
