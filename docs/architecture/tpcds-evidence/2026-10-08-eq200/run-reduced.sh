#!/bin/bash
set -eu
docker run --rm --name eq200-reduced --network none --cpuset-cpus 0-1 --memory 4g --memory-swap 4g --user 1000:1000 --mount type=bind,src="$PWD/build/eq200/programs/releases/v0.1.0-alpha.36.eq200",dst=/runtime,readonly --mount type=bind,src="$PWD/build/eq200",dst=/scripts,readonly --mount type=bind,src=/data2/supabricks-eq/eq200,dst=/reports --workdir /reports sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec /runtime/python/analytics/python /scripts/reduced.py --engine sail --output /reports/reduced-candidate.json > build/eq200/reduced-candidate.log 2>&1
python3 - <<'PY'
import json,hashlib
from pathlib import Path
root=Path('/data2/supabricks-eq/eq200');candidate=root/'reduced-candidate.json';reference=Path('/data2/supabricks-eq/eq200-preflight/spark.json')
a=json.loads(candidate.read_text());b=json.loads(reference.read_text());assert a['fixture_sha256']==b['fixture_sha256'];assert len(a['queries'])==len(b['queries'])==17
checks=[]
for actual,expected in zip(a['queries'],b['queries']):
 assert (actual['id'],actual['sql'])==(expected['id'],expected['sql'])
 checks.append(dict(id=actual['id'],complete=actual['status']==expected['status']=='complete',same_types=[f['type'] for f in actual['schema']['fields']]==[f['type'] for f in expected['schema']['fields']],same_values=actual['rows']==expected['rows']))
passed=all(x['complete'] and x['same_types'] and x['same_values'] for x in checks)
result=dict(status='PASS' if passed else 'FAIL',checks=checks,candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest())
(root/'reduced-comparison.json').write_text(json.dumps(result,indent=2)+'\n');print(result['status'],len(checks));assert passed
PY
