import json,sys,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
import incremental_worker as w
import pyarrow.parquet as pq
pre=Path('/pre');post=Path('/post')
a=json.loads((pre/'work/plan.json').read_text());b=json.loads((post/'work/plan.json').read_text())
assert a==b,'saved plans differ'
planned=b['tables'][0];columns=planned['columns'];names=[c[1] for c in columns]
expected=sorted(w.canonical(row) for key,row in planned['rows'])
results=[]
for root in (pre,post):
 receipt=json.loads((root/'work/result.json').read_text())['descriptor']
 table=next(t for t in receipt['manifest']['tables'] if str(t['oid'])==planned['oid'])
 path=root/'generation'/table['path'];delta=w.DeltaTable(str(path))
 assert table['version']==delta.version()==1
 actions=[json.loads(line) for line in (path/'_delta_log/00000000000000000001.json').read_text().splitlines()]
 assert not any('remove' in act for act in actions)
 actual=[];files=[]
 for act in actions:
  if 'add' not in act:continue
  p=path/act['add']['path'];files.append(p.name)
  for row in pq.read_table(p).select(names).to_pylist():
   actual.append(w.canonical([row[name] for name in names]))
 assert sorted(actual)==expected
 old=w.DeltaTable(str(path),version=0)
 assert set(old.file_uris())<=set(delta.file_uris())
 assert table['rows']==11_010_048+16_384
 results.append(dict(path=str(root),rows=table['rows'],added_rows=len(actual),operation=delta.history(1)[0]['operation'],no_removed_files=True,old_files_retained=True))
print(json.dumps(dict(status='PASS',identical_saved_plan=True,plan_sha256=hashlib.sha256(w.canonical(a)).hexdigest(),exact_added_rows=True,results=results),indent=2))
