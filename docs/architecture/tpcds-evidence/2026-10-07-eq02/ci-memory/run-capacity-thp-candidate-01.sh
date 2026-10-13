#!/bin/bash
set -eu
for suite in capacity; do
 out=/data2/supabricks-eq/eq02-merge-review/installed-$suite-thp-candidate-01
 mkdir -m 700 "$out"
 mkdir -m 700 "$out/tmp"
 docker run --rm --name "eq-ci-$suite" --network none --env _RJEM_MALLOC_CONF=thp:always --cpuset-cpus 0-3 --memory 16g --memory-swap 16g --user 1000:1000 --mount type=bind,src="$PWD",dst=/repo,readonly --mount type=bind,src="$out",dst=/reports --mount type=bind,src="$out/tmp",dst=/tmp -w /repo sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec python3 install/native/catalog_gate.py --timeout 900 --report /reports/cleanup.json -- python3 e2e/native/installed_sync.py --release /repo/build/eq02-merge-review/thp-candidate --suite "$suite" --report /reports/result.json > "build/eq02-merge-review/installed-$suite-thp-candidate-01.log" 2>&1
 python3 - "$out" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);r=json.loads((p/'result.json').read_text());c=json.loads((p/'cleanup.json').read_text())
print(p.name,r['status'],len(r['checks']),c,flush=True)
assert r['status']=='PASS' and not c['exit_code'] and not c['leaked_descendants'] and not c['remaining_descendants']
PY
done
