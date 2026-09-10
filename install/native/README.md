# Native local analytical preview (I01)

The localhost preview uses the same bootstrap and signed archives intended for
`curl -fsSL https://supabricks.io/install.sh | bash`. Domain deployment is deferred.
The default distribution includes the private analytical runtime. `--postgres-only`
selects the smaller Postgres alpha during assembly.

## Try the staged installer

On a host serving the prepared release directory:

```sh
python3 install/native/serve.py --directory build/releases --port 8080
```

In another terminal, as an ordinary user:

```sh
curl -fsSL http://127.0.0.1:8080/install.sh | bash
. "$HOME/.local/share/supabricks/env"
supabricks up
supabricks init my-app
supabricks database create main --wait
supabricks console
supabricks connect main --uri
supabricks sql --branch main --write --sql 'CREATE TABLE orders(id int, amount numeric(12,2))'
supabricks sql --branch main --write --sql 'INSERT INTO orders VALUES (1, 12.99)'
supabricks analytics sql --branch main --sql 'SELECT sum(amount) FROM public.orders'
supabricks spark shell --branch main
```

The installer needs the OS's Bash, curl, OpenSSL, tar and standard shell utilities.
It requires no compiler, Python, Docker, root access, JVM or cloud account.
Supported assembly targets are Linux x86_64 (glibc 2.39+, Ubuntu 24.04 baseline)
and Apple Silicon macOS 15+. Intel Macs, Linux arm64, musl and Windows are not
supported. Installed runtime operation uses loopback and needs no external service.
Application dependencies and your coding agent are supplied by the application
developer; neither is installed as part of the database runtime.

I02 uses version `v0.1.0-alpha.8`. It adds browser CSV/TSV imports to the
PostgreSQL workspace and I01 ingestion service, alongside R03's coordinated
backup/restore and explicit platform upgrades. See the
[recovery and upgrade runbook](../../docs/handbook/recovery.md). Existing R01
Postgres-only installations still require separate program/data directories;
profile conversion and engine upgrades are not qualified.

`supabricks console` starts/reconnects the runtime and opens the current project's
overview. `--no-open` returns a private, single-use browser launch URL as JSON.
The [console runbook](../../docs/handbook/local-console.md) covers source builds,
session expiry and browser support. The [database workspace guide](../../docs/handbook/database-workspace.md) covers branch controls, SQL and saved queries. [CSV/TSV ingestion](../../docs/handbook/csv-ingestion.md) is available through CLI/MCP and the [browser import wizard](../../docs/handbook/browser-imports.md). Try the [synthetic CSV/branch walkthrough](../../examples/console/README.md).

The default program directory is `~/.local/share/supabricks`, separate from the
data root `~/.supabricks`. Set `SUPABRICKS_INSTALL_DIR` to an absolute path before
piping into Bash to change the former; use `SUPABRICKS_DATA_DIR` or `--data-dir`
for the latter. For example:

```sh
curl -fsSL http://127.0.0.1:8080/install.sh |
  SUPABRICKS_INSTALL_DIR="$HOME/Applications/Supabricks Preview" bash
supabricks up --data-dir /tmp/my-supabricks-data
```

The installer appends an idempotent PATH entry to `.profile`, `.bashrc` and
`.zshrc`; `SUPABRICKS_NO_MODIFY_PATH=1` disables this. A child Bash cannot change
its parent terminal's PATH: source the printed env file or open a new terminal.
The private `bin` supplies `supabricks` and `psql`; no system Postgres files or
services are changed. Supabricks allocates its own ports.

## Assembly (build machine only)

Use Python 3.12+, the Rust toolchain, pinned Go 1.27.1, and `patchelf` on Linux. Obtain the pinned
engine archive using the native CI workflow and prepare helpers as documented in
`components/README.md`. Apple Silicon helper compilation additionally needs the
pinned Go toolchain. Prepare release helpers with `--offline-runtime`: unchanged
Process Compose source is built with its upstream update-check option disabled.
No Go compiler is shipped or required by the installer. Then:

```sh
cargo build --locked --release -p supabricks-local
git submodule update --init console
npm ci --prefix console --no-audit --no-fund
npm run build --prefix console
python3 components/prepare-native-cell.py linux-x86_64 build/native --offline-runtime
python3 install/native/assemble.py --target linux-x86_64 \
  --binary target/release/supabricks \
  --engine build/native/supabricks-engine-linux-x86_64 \
  --helpers build/native --output build/releases
python3 install/native/serve.py --directory build/releases
```

Assembly also downloads the checksum-pinned Python distribution and locked
package wheels, prebuilds Spark Connect on the builder, and checks analytical
library dependencies. Users do not supply Python or install packages. See
[the R02 contract](../../docs/architecture/r02-analytical-preview.md).

The manifest records source identities, build environment, helper hashes, OS
baseline, and every shipped file's digest and executable mode. Engine loader
paths are already relocatable; assembly also checks the CLI's library closure.
Release archives contain regular files only. `supabricks installation verify`
checks the inventory without contacting the server.

`serve.py` creates an ephemeral RSA key outside the served directory, signs the
archive checksums, and embeds its public key and exact version in `install.sh`.
The bootstrap verifies the signature before downloading/extracting the payload,
then checks the archive and file inventory before activating the release.
HTTP is permitted only for localhost preview URLs. This local key proves the
packaging flow, not a public publisher identity. For deployment, `stage.py`
accepts an explicitly provisioned signing key and HTTPS base URL. HTTPS delivery
of the bootstrap remains the initial trust boundary.

Repeat installs require identical signed archive bytes and verify existing
files. Concurrent installers fail without replacing an active release. Failed
downloads leave the previous release active. An uncatchable interruption can
leave `.stage.*` and an empty `.install-lock`; remove those staging items only
after confirming no installer is running. Database files are never install inputs.
Version changes require `SUPABRICKS_UPGRADE=1` and `SUPABRICKS_BACKUP_DIR`.
The candidate checks compatibility, stops the previous release, creates a verified
recovery bundle, and activates through a resumable transaction. Downgrades use
restore into a new data root with the exact source release; see the runbook.

After `supabricks down`, the same immutable installation directory can be moved.
Run the moved `bin/supabricks up`; it discovers the engine relative to itself and
rebinds stored executable paths without changing branch ports or credentials.
Source the env file only after rerunning the installer at its new location to
refresh PATH entries. Moving the **data root** is a separate recovery operation.

## Public release gates

This slice is hosted on localhost. Before deploying a public endpoint: qualify
both exact archives, provision a publisher signing key, complete transitive
engine/Go/system-library license notices and corresponding-source distribution,
and configure HTTPS hosting. Primary licenses, platform Cargo dependency notices
and pinned source/build inventories are included now; they are not a completed
redistribution audit. macOS ad-hoc executable signatures are inherited from
native packaging; Developer ID/notarization and downloaded-file Gatekeeper
behavior need separate public-distribution qualification. R03 provides platform upgrades and stopped backup/restore. Actual OS reboot and
power-loss qualification remain required before public durability claims. See the qualification report for
which clean-host/offline tests have actually run.

I00 uses catalog 9. Existing catalog-8 roots require the explicit backed-up
[upgrade workflow](../../docs/handbook/recovery.md); [ingestion commands](../../docs/handbook/csv-ingestion.md) use the same catalog in I01.
