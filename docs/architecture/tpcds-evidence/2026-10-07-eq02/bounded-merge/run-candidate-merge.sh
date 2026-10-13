#!/bin/bash
set -eu
out=/data2/supabricks-eq/eq03-issue184/bounded-merge-mixed-03
mkdir -m 700 "$out"
mkdir -m 700 "$out/tmp"
docker run --rm --name eq184-mixed --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src="$out",dst=/reports --mount type=bind,src="$out/tmp",dst=/tmp -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 900 --report /reports/cleanup.json -- python3 e2e/native/installed_sync.py --release /repo/build/eq03-issue184/bounded-merge-runtime --suite merge --report /reports/result.json > build/eq03-issue184/bounded-merge-mixed-03.log 2>&1
