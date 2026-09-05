# Local component baseline

This tracks **P00** and native engine work in **E01** from the
[local runtime plan](../docs/plans/local-runtime-implementation.md). The
[lock](components.lock.json) records selected sources, unresolved candidates,
owners and target-specific evidence. It is an engineering inventory, not an
installable distribution. No native engine artifact is qualified yet.

## Run the checks

From the repository root with Python 3.12:

```sh
python3 -m venv components/.venv
. components/.venv/bin/activate
python -m pip install -r components/requirements.txt
python components/validate.py
python -m unittest discover -s components -p 'test_*.py' -v
```

With that environment active, `just verify-native-baseline` runs the same two
checks. Dependency installation needs a package index or cache; validation
itself uses only repository files. The validator uses the off-the-shelf
[jsonschema implementation](https://pypi.org/project/jsonschema/4.25.1/) for the
[JSON Schema contract](schemas/components.schema.json), plus cross-file checks.

The separate [native baseline workflow](../.github/workflows/native-baseline.yml)
runs these checks and the [synthetic analytical probe](../spikes/local-analytics/README.md)
on Linux. Existing operator/Kubernetes checks remain in `ci.yml` and their
current Justfile recipes.

## What the lock means

- `selection.kind: git` records a full source SHA. Branch names are descriptive
  only, including the maintained PG candidate; they are not resolved by this tool.
- `package` records the exact version exercised in the analytical fixture.
  The fixture's full dependency set is in its requirements file. These package
  versions alone are insufficient to reconstruct a shipped native environment.
- `unselected` is explicit unresolved work. Bundled SQLite and a private Python
  distribution still need release/build selection. Process Compose and SeaweedFS
  have exact release archives pinned for both targets.
- Each target is `untested`, `probe-passed`, or `qualified`. The historical
  Linux analytics report cannot establish native/macOS qualification, and its
  versions must match both the manifest and the fixture's requirements.
- An artifact records a target, HTTPS location, SHA-256 and a repository-local
  provenance file. A qualified target requires a pin, artifact and evidence.
  Libraries embedded in a larger bundle can reference that bundle and its
  provenance. License identifiers identify the primary component only; full
  bundled dependency notices and license review belong to E01/R02 packaging.

The selected pair is Neon `032d26fb628b4bddfa95e1ced4ffb9e415725bd9`
with PG17.8 gitlink `56692dfb680281a963c7470fc7f0fec7f65ecfd4` from
`sb/REL_17_STABLE`. The original PG17.5 pair was reproduced first, then the Neon
extension was adapted to the maintained fork's SLRU, block-LSN and lock hooks.
No Postgres core patch was added. An engine update
must change the selected PG pin and the recorded gitlink together after checking
the actual Neon source tree. The validator checks local consistency; the native
builder checks fetched Git objects and source cleanliness before building.

Legacy image digests are checked against [chart/values.yaml](../chart/values.yaml)
and never used as native artifact checksums. The local-build operator image is
excluded because it has no upstream digest. The
[repository map](../docs/plans/repository-map.md) explains the source/image gap.

## Qualification boundary

```sh
python components/validate.py --require-qualified
```

This command **currently fails by design**. It requires qualification records
for every component on Linux x86_64 and macOS arm64. It is a metadata completeness
gate, not an installer or a substitute for running the cited tests: it does not
download artifacts, verify their bytes/signatures, audit report contents, or
prove reproducibility. Those checks must be added with E01/R01 release assembly.
Do not fill missing hashes or evidence with placeholders to make it pass.

Native evidence should record the component source/artifact identities, target
OS and architecture, build flags and dependencies, commands, results and limits.
Remove the old synthetic report from a target's qualification evidence when
promoting it and attach the new native report; preserve the historical fixture.
Schema version 2 deliberately fixes the initial target/component scope. Change
the schema and validator together when introducing a new major or release profile.

## Native PG17 developer bundle (E01)

Build and packaging recipes live in
[supabricks/neon](https://github.com/supabricks/neon/pull/1). Its native workflow
builds Linux x86_64 and macOS arm64 without private Neon credentials. Follow
`scripts/native/README.md` there for build dependencies and commands. PG14–16
headers remain build inputs; only PG17 compute and extensions are packaged.

The [Linux report](provenance/native-linux.json) records 224 PostgreSQL regression
tests and eight native runtime checks against the exact selected clean sources.
The bundle was moved into a path containing spaces before ordinary `psql`,
explicit-LSN branching, isolation, compute restart, concurrent GiST reads and
lazy SLRU download checks. This used the LocalFs test remote backend on the build
host. It does not establish S3 durability or clean-host/offline operation.

With the Python environment above active, verify an unpacked engine archive:

```sh
python components/verify-native-bundle.py /path/to/unpacked-engine
```

The verifier checks source pins, clean-build metadata, required binaries, exact
file inventory, checksums and symlink containment. It verifies an existing
manifest's claims; it does not authenticate the publisher or run the engine.

Fetch the separately pinned Process Compose and SeaweedFS archives:

```sh
python components/fetch-native-helpers.py linux-x86_64 /tmp/supabricks-helpers
python components/fetch-native-helpers.py macos-arm64 /tmp/supabricks-helpers
```

Both target downloads have been checksum-verified, and the Linux binaries passed
their version commands. Downloading does not install or launch services. P03
must still qualify supervisor ownership and the S3 backend. Current upstream
PG17 minor integration, macOS and clean-host evidence, transitive license notices
and release signing remain gates. No platform CLI or one-command installer is
implemented by this slice.
