# Local component baseline

This is the first implementation slice (platform portion of **P00**) in the
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
- `unselected` is explicit unresolved work. Process Compose, SeaweedFS, bundled
  SQLite and a private Python distribution still need release/build selection.
- Each target is `untested`, `probe-passed`, or `qualified`. The historical
  Linux analytics report cannot establish native/macOS qualification, and its
  versions must match both the manifest and the fixture's requirements.
- An artifact records a target, HTTPS location, SHA-256 and a repository-local
  provenance file. A qualified target requires a pin, artifact and evidence.
  Libraries embedded in a larger bundle can reference that bundle and its
  provenance. License identifiers identify the primary component only; full
  bundled dependency notices and license review belong to E01/R02 packaging.

The reproduction pair is Neon `d348114f6afc35bbe10e044003128674a6b2b79b`
with its actual PG16 gitlink `a42351fcd41ea01edede1daed65f651e838988fc`.
The newer `sb/REL_16_STABLE` candidate is recorded separately. An engine update
must change the selected PG pin and the recorded gitlink together after checking
the actual Neon source tree. The validator checks local consistency; E01 must
verify fetched Git objects and the source tree before building.

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
Schema version 1 deliberately fixes the initial target/component scope. Change
the schema and validator together when introducing a new major or release profile.

## Next slice: a native PG16 engine bundle (E01)

Add source-build recipes in `supabricks/neon`, consume the exact reproduction
pair above and return artifact/provenance records to this repo. Start with Linux
x86_64, then macOS arm64. The first demonstration must launch the storage cell,
write through ordinary `psql`, branch, terminate/restart compute and read the
persisted data after moving the bundle. Qualify a maintained PG16 minor before
public preview. No native runtime, CLI or installer is implemented in P00.
