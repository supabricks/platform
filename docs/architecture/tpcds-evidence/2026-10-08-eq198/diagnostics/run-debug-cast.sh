#!/bin/bash
set -eu
mkdir -p /data2/supabricks-eq/eq198/debug-cast
docker run --rm --name eq198-debug-cast --network none --cpuset-cpus 0-1 --memory 4g --memory-swap 4g --user 1000:1000 --mount type=bind,src="$PWD/build/eq197/programs/releases/v0.1.0-alpha.36.eq197b",dst=/runtime,readonly --mount type=bind,src="$PWD/build/eq198",dst=/scripts,readonly --mount type=bind,src=/data2/supabricks-eq/eq198/debug-cast,dst=/reports -w /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /runtime/python/runtime/bin/python3.12 -s -B /scripts/debug-cast.py --output /reports/results.json > build/eq198/debug-cast.log 2>&1
