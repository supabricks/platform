#!/bin/bash
set -eu
out=/data2/supabricks-eq/eq03-issue184/bounded-merge-load-07
test -d "$out"
docker run --rm --name eq184-verify --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src="$out",dst=/reports --mount type=bind,src=/data2/supabricks-eq/eq00-20261006/sf1-02,dst=/sf1,readonly -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 18000 --report /reports/verify-cleanup.json -- python3 e2e/tpcds/verify.py --release /repo/build/eq03-issue184/bounded-merge-runtime-02 --inputs /repo/build/eq00-20261006/inputs --load /reports/load --output /reports/product > build/eq03-issue184/bounded-merge-verify-07.log 2>&1
