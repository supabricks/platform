#!/bin/bash
set -eu
bash build/eq206/run-extract.sh
python3 build/eq206/order-envelope.py
python3 - <<'PY'
import hashlib,json
from pathlib import Path
p=Path('e2e/tpcds/q39-review.lock.json');c=json.loads(p.read_text());source=Path('/data2/supabricks-eq/eq206/diagnostics/all-groups.json');f=json.loads(source.read_text())
c['groups_sha256']=hashlib.sha256(source.read_bytes()).hexdigest();c['dimension_key_counts']=f['dimension_key_counts'];p.write_text(json.dumps(c,indent=2)+'\n')
PY
for mode in baseline candidate; do
 python3 build/eq206/review-results.py --root "/data2/supabricks-eq/eq206/$mode"
 python3 e2e/tpcds/q39_review.py --product "/data2/supabricks-eq/eq206/$mode/product" --reference /data2/supabricks-eq/eq02-20261007/reference-02 --groups /data2/supabricks-eq/eq206/diagnostics/all-groups.json --strict "/data2/supabricks-eq/eq206/$mode/comparison.json" --output "/data2/supabricks-eq/eq206/$mode/numerical-review"
done
python3 build/eq206/summarize.py
python3 build/eq206/finalize-report.py
