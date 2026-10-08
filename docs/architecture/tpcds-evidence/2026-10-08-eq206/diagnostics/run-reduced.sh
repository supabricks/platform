#!/bin/bash
set -eu
mode="$1"
runtime="$PWD/build/eq198/programs-final/releases/v0.1.0-alpha.36.eq198c"
extra=()
if [[ "$mode" = candidate* || "$mode" = cluster* ]]; then
 runtime="$PWD/build/eq206/programs/releases/v0.1.0-alpha.36.eq206c"
fi
if [[ "$mode" = cluster* ]]; then extra+=(--env SAIL_MODE=local-cluster); fi
parallelism=2
if [[ "$mode" = *1 ]]; then parallelism=1; fi
if [[ "$mode" = spark* ]]; then
 runtime="$PWD/build/eq03-capacity/runtime-02"
 extra=(--env EQ206_ENGINE=spark --env PYTHONPATH=/reference/lib/python3.12/site-packages --mount type=bind,src="$PWD/build/eq01-171/spark-reference",dst=/reference,readonly)
fi
mkdir -p "/data2/supabricks-eq/eq206/reduced-$mode"
docker run --rm --name "eq206-reduced-$mode" --network none --cpuset-cpus 0-1 --memory 4g --memory-swap 4g --user 1000:1000 --env JAVA_HOME=/runtime/share/unity-catalog/java --env SPARK_LOCAL_IP=127.0.0.1 --env SPARK_LOCAL_DIRS=/reports/spill --env EQ206_PARALLELISM="$parallelism" --env SAIL_EXECUTION__DEFAULT_PARALLELISM="$parallelism" "${extra[@]}" --mount type=bind,src="$runtime",dst=/runtime,readonly --mount type=bind,src="$PWD/build/eq206",dst=/scripts,readonly --mount type=bind,src="/data2/supabricks-eq/eq206/reduced-$mode",dst=/reports --workdir /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /runtime/python/runtime/bin/python3.12 -s -B /scripts/reduced.py > "build/eq206/reduced-$mode.log" 2>&1
