#!/bin/bash
set -eu
mode="$1"
root=/data2/supabricks-eq/eq199
mkdir -p "$root/reduced-$mode"
extra=()
case "$mode" in
spark)
    runtime="$PWD/build/eq200/programs/releases/v0.1.0-alpha.36.eq200"
    engine=spark
    extra=(--env PYTHONPATH=/reference/lib/python3.12/site-packages --mount type=bind,src="$PWD/build/eq01-171/spark-reference",dst=/reference,readonly)
    ;;
baseline) runtime="$PWD/build/eq200/programs/releases/v0.1.0-alpha.36.eq200"; engine=sail;;
candidate) runtime="$PWD/build/eq199/programs/releases/v0.1.0-alpha.36.eq199"; engine=sail;;
*) exit 2;;
esac
docker run --rm --name "eq199-reduced-$mode" --network none --cpuset-cpus 0-1 --memory 4g --memory-swap 4g --user 1000:1000 --env JAVA_HOME=/runtime/share/unity-catalog/java --env SPARK_LOCAL_IP=127.0.0.1 --env SPARK_LOCAL_DIRS=/reports/spill "${extra[@]}" --mount type=bind,src="$runtime",dst=/runtime,readonly --mount type=bind,src="$PWD/build/eq199",dst=/scripts,readonly --mount type=bind,src="$root/reduced-$mode",dst=/reports --workdir /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /runtime/python/runtime/bin/python3.12 -s -B /scripts/reduced.py --engine "$engine" --output /reports/results.json > "build/eq199/reduced-$mode.log" 2>&1
