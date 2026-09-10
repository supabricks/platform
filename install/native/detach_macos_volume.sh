#!/usr/bin/env bash
# Only for the disposable ingestion pressure volume created by native-release.
set -euo pipefail
pressure_mount=${1:?Supply the temporary ingestion pressure mount}
case "$pressure_mount" in
  /tmp/sb-i01-volume.*/*) echo 'Expected the mount root, not a nested path' >&2; exit 1 ;;
  /tmp/sb-i01-volume.*) ;;
  *) echo 'Refusing to detach a path outside the ingestion pressure fixture' >&2; exit 1 ;;
esac

# Processes have exited before this helper runs, but macOS can retain transient
# filesystem references. Retry normal detach, then force only this test volume.
for attempt in 1 2 3; do
  if hdiutil detach "$pressure_mount"; then
    rmdir "$pressure_mount"
    exit 0
  fi
  sleep "$attempt"
done
hdiutil detach -force "$pressure_mount"
rmdir "$pressure_mount"
