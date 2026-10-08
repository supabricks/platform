#!/bin/bash
set -eu
out=/data2/supabricks-eq/eq199/baseline
mkdir -p "$out"
cp -a --reflink=auto /data2/supabricks-eq/eq200/candidate/load "$out/load"
docker run --rm --name eq199-baseline --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src="$PWD/build/eq200/programs",dst=/programs,readonly --mount type=bind,src="$out",dst=/reports -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 18000 --report /reports/verify-cleanup.json -- python3 e2e/tpcds/verify.py --release /programs/releases/v0.1.0-alpha.36.eq200 --load-release /programs/releases/v0.1.0-alpha.36 --inputs /repo/build/eq00-20261006/inputs --load /reports/load --output /reports/product --attach-deployment 9bcac7aa-3644-4478-a0ea-6982d569d5c5 --sail-artifact /repo/build/eq200/sail-artifact
python3 e2e/tpcds/compare.py --product "$out/product" --reference /data2/supabricks-eq/eq02-20261007/reference-02 --output "$out/comparison.json"
python3 build/eq200/review-results.py --root "$out" --reference /data2/supabricks-eq/eq02-20261007/reference-02 --inputs build/eq00-20261006/inputs/spark
