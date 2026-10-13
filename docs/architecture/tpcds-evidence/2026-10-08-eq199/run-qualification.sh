#!/bin/bash
set -eu
test "$1" = candidate
prefix="$PWD/build/eq199/programs"
version=v0.1.0-alpha.36.eq199
artifact=(--sail-artifact /repo/build/eq199/sail-artifact)
out="/data2/supabricks-eq/eq199/$1"
docker run --rm --name "eq199-$1" --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src="$prefix",dst=/programs,readonly --mount type=bind,src="$out",dst=/reports -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 18000 --report /reports/verify-cleanup.json -- python3 e2e/tpcds/verify.py --release "/programs/releases/$version" --load-release /programs/releases/v0.1.0-alpha.36 --inputs /repo/build/eq00-20261006/inputs --load /reports/load --output /reports/product --attach-deployment 9bcac7aa-3644-4478-a0ea-6982d569d5c5 "${artifact[@]}" > "build/eq199/$1-verify.log" 2>&1
python3 e2e/tpcds/compare.py --product "$out/product" --reference /data2/supabricks-eq/eq02-20261007/reference-02 --output "$out/comparison.json" > "build/eq199/$1-compare.log" 2>&1
python3 build/eq199/review-results.py --root "$out" --reference /data2/supabricks-eq/eq02-20261007/reference-02 --inputs build/eq00-20261006/inputs/spark > "build/eq199/$1-review.log" 2>&1
