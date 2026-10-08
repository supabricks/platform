#!/bin/bash
# Reproduction runner for the separately retained #184 diagnostics.
set -eu
mode=$1
trial=$2
runtime=key-pruning-runtime
case "$mode" in
 reconstruct) script=replay-post-compaction.py ;;
 fresh) script=replay-compacted-fresh.py ;;
 baseline) script=replay-compacted-baseline.py; runtime=row-prefix-runtime ;;
 *) exit 2 ;;
esac
out=/data2/supabricks-eq/eq02-20261007/post-compaction-replay-$trial
mkdir -m 700 "$out"
status=0
docker run --rm --name eq-post-compaction --network none --cpuset-cpus 0-7 --memory 2g --memory-swap 2g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src=/data2/supabricks-eq/eq02-20261007/key-pruning-load-05,dst=/failed,readonly --mount type=bind,src=/data2/supabricks-eq/eq02-20261007/post-compaction-replay-02,dst=/compacted,readonly --mount type=bind,src="$out",dst=/diag -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec timeout 45 "build/eq02-20261007/$runtime/python/analytics/python" "build/eq02-20261007/$script" > "build/eq02-20261007/post-compaction-replay-$trial.log" 2>&1 || status=$?
python3 - "$out" "$status" <<'PY'
import json,sys
from pathlib import Path
(Path(sys.argv[1])/'termination.json').write_text(json.dumps(dict(exit_code=int(sys.argv[2]),timeout_seconds=45))+'\n')
PY
exit "$status"
