#!/bin/bash
set -eu
mode=$1
trial=$2
out=/data2/supabricks-eq/eq02-20261007/investigate-182-$mode-$trial
mkdir -m 700 "$out"
docker run --rm --name "eq182-$mode-$trial" --network none --cpuset-cpus 0-7 --memory 2g --memory-swap 2g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src=/data2/supabricks-eq/eq02-20261007/load-04/state,dst=/state,readonly --mount type=bind,src="$PWD/docs/architecture/tpcds-evidence/2026-10-07-eq02",dst=/evidence,readonly --mount type=bind,src="$out",dst=/diag -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec build/eq02-20261007/row-prefix-runtime/python/analytics/python build/eq02-20261007/investigate-182.py "$mode" > "build/eq02-20261007/investigate-182-$mode-$trial.log" 2>&1
cat "$out/result.json"
