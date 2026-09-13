# Back up, restore and upgrade the localhost preview

I03 uses `v0.1.0-alpha.14` with catalog 10. Linux x86_64 and Apple
Silicon macOS use their own native bundles. The release gate upgrades the exact
PR34 alpha.8 catalog-9 archive and the NE05 alpha.12 archive with existing notebook environments; migration fixtures also cover catalog 8.
See the [environment manager](../architecture/ne02-environment-manager.md) and
[kernel binding contract](../architecture/ne03-kernel-environments.md).

## Make a recovery bundle

Choose a new directory outside the data root, with enough free space for a full
copy of the runtime data. The command stops the cell and leaves it stopped:

```sh
supabricks backup create "$HOME/supabricks-backup-001"
supabricks backup verify "$HOME/supabricks-backup-001"
supabricks up
```

Backups contain database credentials and private keys. Keep the directory private;
it is not encrypted. Application source and `supabricks.toml` outside the data
root need their own backup, including `notebooks/environment/pyproject.toml` and
`uv.lock`. Materialized notebook environments and their caches are disposable and
excluded from backups; prepare them again after restore. For offline recovery, also export an explicit [environment wheel bundle](notebook-environments.md) before stopping the cell. Copying a live `~/.supabricks` is not a supported backup.

## Restore into a new directory

Stop the original runtime to free its retained ports. Restore with the source
release, then point subsequent commands at the new root:

```sh
supabricks down
supabricks backup restore "$HOME/supabricks-backup-001" \
  --data-dir "$HOME/.supabricks-restored"
export SUPABRICKS_DATA_DIR="$HOME/.supabricks-restored"
supabricks up
```

Use the same application project directory. Confirm both application queries and
analytical epochs before retiring any original data. Existing target directories
are refused. A failed restore is never merged into an existing cell: use another
new destination and retain the partial directory until you have inspected it.

## Upgrade to NE06

Serve the prepared alpha.13 release directory with `install/native/serve.py` as in
the installer quickstart. In the client terminal:

```sh
curl -fsSL http://127.0.0.1:8080/install.sh |
  SUPABRICKS_UPGRADE=1 \
  SUPABRICKS_BACKUP_DIR="$HOME/supabricks-before-alpha13" bash
supabricks up
```

Set `SUPABRICKS_INSTALL_DIR` and `SUPABRICKS_DATA_DIR` on that Bash invocation if
you used custom directories. The installer verifies the new release, stops the
old cell, backs it up and activates the candidate. The runtime stays stopped until
`up`. The verified stopped backup precedes the explicit catalog-8-or-9-to-10 migration.
Any other incompatible schema, engine, dependency set, target, profile or downgrade
is rejected. This is a platform upgrade with the same PG17 engine.

For an interrupted activation, rerun the same installer and options. Never delete
`upgrade.json` to force startup. If Bash was killed before its exit trap ran, it
may leave `.install-lock` and `.stage.*` inside the program directory. First confirm
that the installer and all its child processes have stopped; remove only that
empty lock directory, then retry. Partial stage directories are not active releases.
If a prepared backup is incomplete or the old cell was used after interruption,
use a new `SUPABRICKS_BACKUP_DIR` while the catalog still has its source version; retain the earlier
backup/partial copy. Once migration has committed, the original verified backup
is required to finish activation.

## Return to the pre-upgrade state

Use the new recovery tool to restore the old bundle, selecting the retained old
release. Start that release against a new data root; do not downgrade the active
data root:

```sh
supabricks down
supabricks backup restore "$HOME/supabricks-before-alpha13" \
  --release "$HOME/.local/share/supabricks/releases/v0.1.0-alpha.8" \
  --data-dir "$HOME/.supabricks-before-alpha13-restored"
"$HOME/.local/share/supabricks/releases/v0.1.0-alpha.8/bin/supabricks" up \
  --data-dir "$HOME/.supabricks-before-alpha13-restored"
```

## Uninstall and failure diagnosis

`supabricks installation uninstall` stops the selected cell and removes its active
command links. Data, backups and immutable release directories remain. The same
signed installer can restore the command links. No other data roots are scanned.

`supabricks doctor` and `operation get ID` expose bounded runtime/operation state.
Raw `daemon.log`, engine logs and backup payloads are private and can contain SQL
or credentials. Share the qualification JSON or redact diagnostics before sharing.
For an ambiguous surviving process, use the owning release's `down`; do not delete
`owner.lock`, remove PID records, or kill processes by a global executable name.
Missing control metadata requires a complete recovery bundle, not a fresh journal
created over existing storage files.

Process-kill recovery is tested separately from actual OS reboot and power loss.
The preview does not yet carry a power-loss durability claim. See the
[R03 architecture and evidence boundaries](../architecture/r03-recovery-upgrades.md).
