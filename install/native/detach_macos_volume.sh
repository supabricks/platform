#!/usr/bin/env bash
# Only for the disposable ingestion pressure volume created by native-release.
set -euo pipefail
pressure_mount=${1:?Supply the temporary ingestion pressure mount}
case "$pressure_mount" in
  /tmp/sb-i01-volume.*/*) echo 'Expected the mount root, not a nested path' >&2; exit 1 ;;
  /tmp/sb-i01-volume.*) ;;
  *) echo 'Refusing to detach a path outside the ingestion pressure fixture' >&2; exit 1 ;;
esac

# A busy detach can finish asynchronously. Check device identity before retrying
# so a volume that has already gone away is successful cleanup, not a new error.
unmounted() {
  local mount_device parent_device
  if [[ ! -e "$pressure_mount" ]]; then
    return 0
  fi
  [[ ! -L "$pressure_mount" ]] || return 1
  mount_device=$(stat -f %d "$pressure_mount") || return 1
  parent_device=$(stat -f %d "$(dirname "$pressure_mount")") || return 1
  [[ "$mount_device" == "$parent_device" ]]
}
finish() {
  if [[ -d "$pressure_mount" ]]; then
    rmdir "$pressure_mount"
  fi
}

# Processes have exited before this helper runs, but macOS can retain transient
# filesystem references. Retry normal detach, then force only this test volume.
for attempt in 1 2 3; do
  if unmounted || hdiutil detach "$pressure_mount"; then
    finish
    exit 0
  fi
  sleep "$attempt"
done
if ! unmounted; then
  hdiutil detach -force "$pressure_mount" || unmounted
fi
finish
