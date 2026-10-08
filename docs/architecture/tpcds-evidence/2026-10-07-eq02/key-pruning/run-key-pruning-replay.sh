#!/bin/bash
set -eu
for trial in 01 02 03; do
 out=/data2/supabricks-eq/eq02-20261007/key-pruning-replay-$trial
 mkdir -m 700 "$out"
 docker run --rm --name "eq182-replay-$trial" --network none --cpuset-cpus 0-7 --memory 2g --memory-swap 2g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src=/data2/supabricks-eq/eq02-20261007/load-04/state,dst=/state,readonly --mount type=bind,src="$PWD/docs/architecture/tpcds-evidence/2026-10-07-eq02",dst=/evidence,readonly --mount type=bind,src="$out",dst=/diag -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec build/eq02-20261007/key-pruning-runtime/python/analytics/python build/eq02-20261007/verify-key-pruning-replay.py packaged > "build/eq02-20261007/key-pruning-replay-$trial.log" 2>&1
 python3 - "$out/result.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]));assert r['status']=='PASS' and r['plan_sha256']=='9877790a8fd5e7c4f1b4e2d816fa3ba810316cb90d360714c355d81de60b43ea'
print(sys.argv[1],r['elapsed_seconds'],max(e['highwater'] for e in r['events'])/2**20,flush=True)
PY
done
