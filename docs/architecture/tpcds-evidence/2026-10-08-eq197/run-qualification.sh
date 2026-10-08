#!/bin/bash
set -eu
mode="$1"
case "$mode" in
baseline)
  prefix="$PWD/build/eq199/programs"
  version=v0.1.0-alpha.36.eq199
  state=/data2/supabricks-eq/eq199/candidate
  extra=()
  profile=compact
  ;;
candidate)
  prefix="$PWD/build/eq197/programs"
  version=v0.1.0-alpha.36.eq197b
  state=/data2/supabricks-eq/eq197/candidate
  extra=(--platform-artifact /repo/build/eq197/platform-artifact)
  profile=analytical
  ;;
*) exit 2;;
esac
out="/data2/supabricks-eq/eq197/$mode"
docker run --rm --name "eq197-$mode" --network none --cpuset-cpus 0-7 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src="$prefix",dst=/programs,readonly --mount type=bind,src="$state",dst=/reports --mount type=bind,src="$out",dst=/evidence -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 18000 --report /evidence/verify-cleanup.json -- python3 e2e/tpcds/verify.py --release "/programs/releases/$version" --load-release /programs/releases/v0.1.0-alpha.36 --inputs /repo/build/eq00-20261006/inputs --load /reports/load --output /evidence/product --attach-deployment 9bcac7aa-3644-4478-a0ea-6982d569d5c5 --sail-artifact /repo/build/eq199/sail-artifact --resource-profile "$profile" --sample-resources "${extra[@]}" > "build/eq197/$mode-verify.log" 2>&1
python3 e2e/tpcds/compare.py --product "$out/product" --reference /data2/supabricks-eq/eq02-20261007/reference-02 --output "$out/comparison.json" > "build/eq197/$mode-compare.log" 2>&1
