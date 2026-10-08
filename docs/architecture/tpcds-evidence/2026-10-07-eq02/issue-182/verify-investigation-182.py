"""Compare every diagnostic's exact added rows and retained prefix outside timings."""
import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,'/repo/build/eq02-20261007/row-prefix-runtime/python/analytics')
import incremental_worker as w
base=Path('/runs');d=json.load(open('/evidence/load-04/last-publication.json'))['descriptor']
t=next(t for t in d['manifest']['tables'] if t['name']=='customer_demographics')
original=Path('/state')/d['generation']/t['path']
old={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in original.iterdir() if p.is_file()}
results=[];expected=None
for path in sorted(base.glob('investigate-182-*')):
 if not (path/'result.json').exists():continue
 report=json.loads((path/'result.json').read_text());table=path/'analytics/incremental/generation'/t['path']
 for name,sha in old.items():assert hashlib.sha256((table/name).read_bytes()).hexdigest()==sha,(path,name)
 actions=[json.loads(line) for line in (table/'_delta_log'/f'{20:020}.json').read_text().splitlines()]
 assert not any('remove' in action for action in actions)
 added=[table/a['add']['path'] for a in actions if 'add' in a]
 data=w.ds.dataset([str(p) for p in added],format='parquet').to_table()
 rows=sorted(data.to_pylist(),key=lambda row:row['cd_demo_sk'])
 assert len(rows)==16384
 if expected is None:expected=(data.schema,rows)
 assert data.schema.equals(expected[0],check_metadata=True) and rows==expected[1],path
 results.append(dict(trial=path.name,rows=len(rows),unchanged_prefix_files=len(old),row_sha256=hashlib.sha256(w.canonical(rows)).hexdigest()))
assert len(results)==16
print(json.dumps(dict(status='PASS',scope='Exact diagnostic-to-diagnostic added row/schema equality and unchanged original physical prefix; not source/Delta full SF1 qualification',trials=results),indent=2))
