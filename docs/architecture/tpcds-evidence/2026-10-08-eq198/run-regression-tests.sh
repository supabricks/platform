#!/bin/bash
set -eu
mode="$1"
pytest_extra=()
mkdir -p "/data2/supabricks-eq/eq198/regression-$mode"
case "$mode" in
spark)
    pytest_extra=(--correlated-source dataframe)
    runtime="$PWD/build/eq03-capacity/runtime-02"
    extra=(--env EQ200_ENGINE=spark --env PYTHONPATH=/test-deps:/reference/lib/python3.12/site-packages --mount type=bind,src="$PWD/build/eq01-171/spark-reference",dst=/reference,readonly)
    ;;
baseline|candidate|cluster)
    runtime="$PWD/build/eq197/programs/releases/v0.1.0-alpha.36.eq197b"
    if [ "$mode" != baseline ]; then runtime="$PWD/build/eq198/programs-final/releases/v0.1.0-alpha.36.eq198c"; fi
    extra=(--env PYTHONPATH=/test-deps)
    if [ "$mode" = cluster ]; then extra+=(--env SAIL_MODE=local-cluster); fi
    ;;
*) exit 2;;
esac
docker run --rm --name "eq198-regression-$mode" --network none --cpuset-cpus 0-1 --memory 4g --memory-swap 4g --user 1000:1000 --env JAVA_HOME=/runtime/share/unity-catalog/java --env SPARK_LOCAL_IP=127.0.0.1 --env SPARK_LOCAL_DIRS=/reports/spill --env SAIL_EXECUTION__DEFAULT_PARALLELISM=2 "${extra[@]}" --mount type=bind,src="$runtime",dst=/runtime,readonly --mount type=bind,src="$PWD/build/eq198",dst=/scripts,readonly --mount type=bind,src="$PWD/build/eq200/test-deps",dst=/test-deps,readonly --mount type=bind,src="/data2/supabricks-eq/eq198/regression-$mode",dst=/reports --workdir /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /runtime/python/runtime/bin/python3.12 -s -B -m pytest /scripts/regression-tests -q "${pytest_extra[@]}" -p no:cacheprovider --junitxml=/reports/pytest.xml > "build/eq198/regression-$mode.log" 2>&1
