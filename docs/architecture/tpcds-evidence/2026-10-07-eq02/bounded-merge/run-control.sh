#!/bin/bash
set -eu
mode=$1
trial=$2
runtime=${3:-build/eq02-20261007/key-pruning-runtime}
out=/data2/supabricks-eq/issue184-$mode-$trial
mkdir -m 700 "$out"
status=0
docker run --rm --name "eq184-$mode-$trial" --network none --cpuset-cpus 0-7 --memory 2g --memory-swap 2g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src=/data2/supabricks-eq/eq02-20261007/key-pruning-load-05,dst=/failed,readonly --mount type=bind,src=/data2/supabricks-eq/eq02-20261007/post-compaction-replay-02,dst=/compacted,readonly --mount type=bind,src="$out",dst=/diag -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec timeout 35 "$runtime/python/analytics/python" build/eq03-issue184/controlled-merge.py "$mode" > "build/eq03-issue184/$mode-$trial.log" 2>&1 || status=$?
python3 - "$out" "$status" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);r=dict(exit_code=int(sys.argv[2]),timeout_seconds=35)
(p/'termination.json').write_text(json.dumps(r)+'\n');print(p.name,r)
if (p/'result.json').exists():print((p/'result.json').read_text())
PY
