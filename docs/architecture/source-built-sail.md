# Controlled Sail source and native builds

Platform #45 replaces the deployed upstream `pysail` wheel with a native build
from [supabricks/sail](https://github.com/supabricks/sail). The maintained
`sb/main` branch starts at upstream `v0.7.1`, commit
`9544c9253e981a82c5f9e493c43ce98a4d9d41b7`. The initial fork changes no engine
semantics. Future fixes and upgrades belong in that repository; platform consumes
reviewed immutable commits, never a floating branch or tag.

## Build and ownership

`components/sail-source.lock.json` pins the fork commit, source file hashes,
Rust 1.96.0, Maturin 1.15.0 and checksum-verified protoc 36.1 per target.
Cargo builds with `--locked`. The optimized release profile retains upstream
optimization level 3 but disables LTO and uses 16 codegen units with two build
jobs to bound compiler memory on standard native runners. These choices are
recorded build inputs, not a claim of identical performance or bit-for-bit
reproduction of the upstream wheel. Build downloads require network access;
installed execution does not.

Platform's `source-sail` CI matrix checks out the fork and builds on Linux
x86_64 and macOS arm64. A separate assembly job consumes that run's artifact,
checks the source/build contract, wheel target, full artifact file inventory
and hashes, and installs that exact wheel with package resolution disabled.
A stale cached PyPI Sail wheel is excluded. Missing or invalid source artifacts
fail assembly; there is no PyPI fallback for deployed Sail.

The wheel retains package version `0.7.1` for the existing compatibility locks.
The fork commit, toolchain/profile and wheel SHA-256 identify the actual build.
The old `uv.lock` PyPI entries remain the immutable A00 comparison fixture;
`native-baseline` still tests that historical upstream package. They are not
release authority for Sail. The native-release browser, analytical, notebook,
recovery and lifecycle suites test the source-built engine actually shipped.
Notebook kernels use Spark Connect; they do not install a second Sail engine.

The artifact preserves Sail's Apache-2.0 license, Cargo.lock, dependency source
identities and available license/notice files. Assembly includes these under
`provenance/sail` and `licenses/sail`; immutable release inventory covers them.
`release.json` carries source-build provenance, and the R04 collector rejects
missing, dirty, stale or mismatched Sail build records. Existing relocation and
native-library checks apply to the newly compiled extension. The package
inventory is not a completed transitive public redistribution audit.

## Reproduce and qualify

On a supported native builder with C/C++ build tools and CMake, install the
pinned Rust toolchain, Python 3.12,
and the hashed Maturin requirement, then use a clean checkout of the fork:

```sh
rustup toolchain install 1.96.0 --profile minimal
python3 -m pip install --require-hashes --no-deps --only-binary=:all: -r components/sail-build-requirements.txt
git clone https://github.com/supabricks/sail.git build/sail-source
git -C build/sail-source checkout 9544c9253e981a82c5f9e493c43ce98a4d9d41b7
python3 components/build-sail.py --source build/sail-source --target linux-x86_64 --output build/sail-artifacts/linux-x86_64
```

Use `macos-arm64` on Apple Silicon. Output must be a fresh directory. Source and
build tools are build-machine inputs; end users still install the prepared
native archive with one localhost curl command and need no Rust/compiler setup.
The script records build duration and actual compiler identity.

Alpha.17 retains catalog 10, PG17.8, the merged console pin, the analytics API
and notebook environments. Completion requires the full native-release matrix
on the final PR revision, including source builds and the combined evidence
collector. The final PR report records exact archive/wheel hashes and measured
results; alpha.16's successful reports cannot qualify this changed engine build.

## Future updates

Make a Sail change through a reviewed PR in `supabricks/sail`, retain the
upstream commit and rationale, and advance the platform source lock deliberately.
Keep public API/package-version changes coordinated with analytics/notebook
locks. Every source or build-profile change must rebuild both targets and pass
actual installed workflows before deployment. A fork gives us control of that
sequence; it does not require replacing Sail's planner/executor immediately.
