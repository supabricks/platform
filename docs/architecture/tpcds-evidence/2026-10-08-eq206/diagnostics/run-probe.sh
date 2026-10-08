#!/bin/bash
set -eu
mkdir -p /data2/supabricks-eq/eq206/probes
docker run --rm --name "eq206-probe-$1" --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD/build/eq206/programs/releases/v0.1.0-alpha.36.eq206c",dst=/runtime,readonly --mount type=bind,src="$PWD/build/eq206",dst=/scripts,readonly --mount type=bind,src=/data2/supabricks-eq/eq198/candidate/load,dst=/load,readonly --mount type=bind,src="$PWD/build/eq00-20261006/inputs/spark",dst=/inputs,readonly --mount type=bind,src=/data2/supabricks-eq/eq206/probes,dst=/reports -w /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /runtime/python/runtime/bin/python3.12 -s -B /scripts/probe.py --profile "$1" > "build/eq206/probe-$1.log" 2>&1
