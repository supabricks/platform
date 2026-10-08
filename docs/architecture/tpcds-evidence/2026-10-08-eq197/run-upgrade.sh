#!/bin/bash
set -eu
mkdir -p /data2/supabricks-eq/eq197/backups
docker run --rm --name eq197-upgrade --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD/build/eq197/programs",dst=/programs --mount type=bind,src=/data2/supabricks-eq/eq197/candidate,dst=/reports --mount type=bind,src=/data2/supabricks-eq/eq197/backups,dst=/backups -w /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /programs/releases/v0.1.0-alpha.36.eq197b/bin/supabricks installation upgrade --data-dir /reports/load/state --prefix /programs --previous /programs/releases/v0.1.0-alpha.36 --backup /backups/pre-eq197 > build/eq197/upgrade-result.json 2> build/eq197/upgrade.log
