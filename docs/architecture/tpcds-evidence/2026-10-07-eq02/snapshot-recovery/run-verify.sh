#!/bin/bash
set -eu
out=/data2/supabricks-eq/eq03-recovery/qualification-01
docker run --rm --name eq196-verify --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src="$PWD/build/eq03-recovery/programs",dst=/programs,readonly --mount type=bind,src="$out",dst=/reports -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 18000 --report /reports/verify-cleanup.json -- python3 e2e/tpcds/verify.py --release /programs/releases/v0.1.0-alpha.36.eq196 --load-release /programs/releases/v0.1.0-alpha.36 --inputs /repo/build/eq00-20261006/inputs --load /reports/load --output /reports/product > build/eq03-recovery/verify.log 2>&1
python3 e2e/tpcds/compare.py --product "$out/product" --reference /data2/supabricks-eq/eq02-20261007/reference-02 --output "$out/comparison.json" > build/eq03-recovery/compare.log 2>&1
