#!/bin/bash
set -eu
for variant in predecessor candidate; do
 out=/data2/supabricks-eq/eq03-capacity/replay-$variant-01
 mkdir -m 700 "$out"
 if [ "$variant" = predecessor ]; then runtime=eq03-append; else runtime=eq03-capacity; fi
 set +e
 docker run --rm --name "eq193-$variant" --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src=/data2/supabricks-eq/eq03-append/load-10,dst=/failed,readonly --mount type=bind,src="$out",dst=/diag -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 330 --report /diag/cleanup.json -- "/repo/build/$runtime/runtime-01/python/analytics/python" build/eq03-capacity/replay-load10.py > "build/eq03-capacity/replay-$variant-01.log" 2>&1
 code=$?
 set -e
 echo "$variant exit $code"
 if [ "$variant" = predecessor ]; then test "$code" -eq 1; else test "$code" -eq 0; fi
 done
