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

The selected pair is Neon `1c6fa095261112aae239beef5a221b484703d49a`
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

The [Linux](provenance/native-linux.json) and [macOS](provenance/native-macos.json)
reports record 224 PostgreSQL regressions and nine native runtime checks per
target against the selected clean sources. The archives were moved into a path
containing spaces before ordinary `psql`, explicit-LSN branching, isolation,
compute restart, concurrent GiST reads and lazy SLRU download checks. Core dumps
were disabled with a zero hard limit throughout the runtime tests.

Linux also passed the same workflow inside minimal Ubuntu 24.04 userspace as an
unprivileged user, without build tools or external networking. Its
[package inventory](provenance/linux-runtime-packages.txt) records that environment.
The tested Linux baseline is glibc 2.39; older distributions are unqualified.
macOS 15 arm64 passed on a build-capable runner and still needs separate
clean-host/offline qualification. All storage checks use the LocalFs test remote
backend; they do not establish S3 or power-loss durability.

The reports distinguish the immutable archive build from the corrected runtime
qualification harness. Replaying an archive does not change its source identity.
The [Postgres source-CI report](provenance/postgres-source-ci.json) separately
records the branch-trigger fix, pinned PC ledger audit and 224 core regressions.

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
their version commands. Downloading does not install or launch services. Platform
uses the binaries' embedded Go source revisions for the helper pins; Process
Compose's release tag points to the immediately preceding commit, documented in
its [provenance](provenance/process-compose.json). The validator rejects source
or archive identities that disagree with these records. These are the original
P00 helper probes. P03 uses a different SeaweedFS build, described below. Current upstream
PG17 minor integration, macOS clean-host evidence, transitive license notices
and release signing remain gates. No platform CLI or one-command installer is
implemented by E01.

## Native cell helpers (P03)

The [native cell lock](native-cell.lock.json) pins the engineering engine archives
and SQLite-enabled SeaweedFS variant used by P03. Process Compose remains pinned
by the original component manifest. The default SeaweedFS archive omits SQLite;
its LevelDB metadata path does not request synchronous writes. The native profile
uses the upstream Linux `full` archive or builds unchanged upstream source with
its `sqlite` tag on Apple Silicon. No storage implementation was rewritten.

```sh
python components/prepare-native-cell.py linux-x86_64 /tmp/sb-helpers
```

Use `macos-arm64` with the pinned Go build toolchain on an Apple Silicon build
host. Go is not a runtime dependency. The script verifies archive checksums and
writes `helper-build.json` with source and binary identities. The
[native qualification suite](../e2e/native/README.md) exercises supervision,
authentication, actual S3 operations, disk-full behavior and cold restore.
These engineering archives still need a stable release channel and separate
power-loss qualification before public durability or installer claims.
