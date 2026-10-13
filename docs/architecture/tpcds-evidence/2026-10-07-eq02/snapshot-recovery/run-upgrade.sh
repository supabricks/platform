#!/bin/bash
set -eu
root=/data2/supabricks-eq/eq03-recovery
docker run --rm --name eq196-upgrade --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD/build/eq03-recovery/programs",dst=/programs --mount type=bind,src="$root/qualification-01",dst=/reports --mount type=bind,src="$root/backups",dst=/backups -w /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /programs/releases/v0.1.0-alpha.36.eq196/bin/supabricks installation upgrade --data-dir /reports/load/state --prefix /programs --previous /programs/releases/v0.1.0-alpha.36 --backup /backups/pre-eq196 > build/eq03-recovery/upgrade-result.json 2> build/eq03-recovery/upgrade.log
