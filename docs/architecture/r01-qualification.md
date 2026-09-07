# R01 localhost installer qualification

The R01 implementation assembles a **local Postgres engineering alpha** in
`supabricks/platform`. Public `supabricks.io` hosting is deferred at the user's
request. The localhost server renders the exact curl-to-Bash flow using a
disposable signing key; no public publisher key or hosting credentials are needed.

## Distribution contract

- Linux x86_64/glibc 2.39 and macOS 15 arm64 use the pinned E01 PG17.8 engine.
- Programs install per-user under `~/.local/share/supabricks`, independently of
  the existing `~/.supabricks` database root. Both locations are overridable.
- Version-pinned signed checksums precede download verification, archive path/type
  validation, complete file inventory verification and atomic activation.
- Repeat installation verifies identical bytes. It does not replace system
  Postgres or edit database files. No target-side compiler/Python/Docker is used.
- Runtime paths derive from the executable. The exact immutable release may be
  relocated after shutdown while preserving database state, credentials and ports.
  A different manifest identity is rejected for an existing installed data root;
  general upgrades/downgrades remain R03 work.
- Process Compose is rebuilt from its same pinned upstream source with the
  existing `CheckForUpdates=false` linker option. Upstream binary releases enable
  GitHub checks and have no runtime opt-out. SeaweedFS telemetry stays disabled.
- Supabricks code is Apache-2.0. Bundles include primary engine/helper licenses,
  platform dependency notices and pinned source/build inventories. Full transitive
  engine/Go/system-library redistribution audit remains a public-release gate.

## Qualification procedure

`install/native/test_installer.py` exercises signatures, checksum corruption,
signed malicious paths/symlinks, repeat installs, concurrent ownership, changed
release bytes, shell quoting and preservation of user-owned binaries.

`install/native/qualify.py` downloads the installer with curl and pipes it into
Bash. It uses the installed binary, automatic engine discovery and bundled psql
to run the real orders HTTP application, create a branch, apply an isolated
migration, initialize MCP, wake a suspended branch, relocate a stopped release,
restart, compare stable credentials/data and clean up. The conventional 5432 port
is occupied throughout. The application test supplies its own Python/psycopg.

The native-release workflow assembles both architectures and hands the exact
archives to separate qualification jobs. Linux runs in minimal Ubuntu 24.04
userspace as an ordinary user with `--network=none`, no build tools, and an
existing system psql. `strace` records network syscalls for the harness and its
descendants; the observation gate rejects non-loopback destinations, including
failed telemetry attempts. Raw traces can contain disposable test credentials
and are not uploaded as public artifacts; only the destination summary is.

macOS uses a separate native runner with Seatbelt external-network denial and
Homebrew-library denial. This establishes archive/runtime isolation on an Apple
Silicon host; it is not proof of installation on a factory-clean Mac or of
browser-download Gatekeeper/notarization behavior.

## Findings

The occupied-port test exposed a pre-existing control-connection bug. The URI
`postgresql://cloud_admin@localhost/postgres?host=/private/socket&port=N` is parsed
by tokio-postgres as **two** destinations. It first tries `localhost:5432`, then
the Unix socket. A listener accepting TCP without speaking Postgres hung initial
configuration; a real system database could have been contacted. R01 now encodes
the private socket as the sole URI authority. A parser regression test asserts
one Unix host and exactly the allocated port. The packaged Linux workflow passed
after this fix, including a listening 5432 socket and relocation.

Native macOS testing also exposed a control-socket portability defect. BSD
accept inherits the listener's nonblocking mode; the daemon used blocking
request framing without clearing that flag. A client sending after accept could
receive an unavailable response or a closed socket. Accepted control streams now
explicitly use blocking mode with the existing bounded read/write deadlines. A
regression test connects, delays, and sends a fragmented request. Redacted OS and
SQLite diagnostics now accompany unexpected unavailable responses in daemon.log.

Initial local development artifact: 263,544,433 compressed bytes; 714,464,465
unpacked bytes. These are engineering measurements, not published release sizes.
The installer completed in 16.26 seconds on the development host's loopback
server; no internet-download performance claim is made.

CI artifact identities and final qualification outcomes are recorded in the PR
and the machine-readable native-release reports. Public deployment, publisher key
provisioning, full redistribution audit, factory-clean macOS qualification,
public recovery/upgrade guarantees and the full interactive Codex usability test
remain explicitly outside this localhost delivery.
