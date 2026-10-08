#!/bin/bash
set -eu
mkdir -p /data2/supabricks-eq/eq206/diagnostics
docker run --rm --name eq206-extract-all --network none --cpuset-cpus 0-7 --memory 4g --memory-swap 4g --user 1000:1000 --mount type=bind,src="$PWD/build/eq198/programs-final/releases/v0.1.0-alpha.36.eq198c",dst=/runtime,readonly --mount type=bind,src="$PWD/build/eq206",dst=/scripts,readonly --mount type=bind,src=/data2/supabricks-eq/eq198/candidate/load,dst=/load,readonly --mount type=bind,src=/data2/supabricks-eq/eq02-20261007/reference-02,dst=/reference,readonly --mount type=bind,src=/data2/supabricks-eq/eq206/diagnostics,dst=/reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /runtime/python/runtime/bin/python3.12 -s -B /scripts/extract-all-groups.py > build/eq206/extract-all.log 2>&1
python3 build/eq206/oracle.py
