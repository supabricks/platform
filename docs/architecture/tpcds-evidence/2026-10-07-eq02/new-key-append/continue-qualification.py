import json,subprocess,time
from pathlib import Path
root=Path('/data2/supabricks-eq/eq03-append/load-10')
while not (root/'cleanup.json').exists() or not (root/'apply-memory.json').exists():time.sleep(5)
load=json.loads((root/'load/result.json').read_text());cleanup=json.loads((root/'cleanup.json').read_text())
if load['status']!='PASS' or cleanup['exit_code'] or cleanup['leaked_descendants'] or cleanup['remaining_descendants']:
 print('Load did not pass; verification not started',load['status'],flush=True);raise SystemExit(1)
print('Load and cleanup passed; starting exact tables and product SQL',flush=True)
subprocess.run(['bash','build/eq03-append/run-verify.sh'],check=True)
subprocess.run(['python3','e2e/tpcds/compare.py','--product',str(root/'product'),'--reference','/data2/supabricks-eq/eq02-20261007/reference-02','--output',str(root/'comparison.json')],check=True)
print((root/'comparison.json').read_text(),flush=True)
