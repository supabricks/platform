import json,sys,time,resource
from pathlib import Path
sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
import incremental_worker as w
from capture.spool import canonical
import hashlib
root=Path('/retained/generation');plan=json.loads(Path('/retained/work/plan.json').read_text());receipt=json.loads((root/'compaction.json').read_text())
# Existing replay committed version1; use an independent directory copy, never mutate evidence.
import shutil
out=Path('/diag/generation');shutil.copytree(root,out)
# Retain the selected compacted version0; remove only this copy's later commit/data.
for t in receipt['tables']:
 p=out/t['path'];d=w.DeltaTable(str(p),version=t['version']);keep=set(d.file_uris())
 for f in p.glob('*.parquet'):
  if str(f) not in keep:f.unlink()
 for f in (p/'_delta_log').glob('*.json'):
  if int(f.stem)>t['version']:f.unlink()
config=dict(id=plan['run_id'],deadline_ms=int(time.time()*1000)+90000)
selected=plan['tables'][0];table=next(t for t in receipt['tables'] if str(t['oid'])==selected['oid'])
def mark(name):print(name,time.monotonic()-start,resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,flush=True)
start=time.monotonic();mark('start');result=w.apply_table(config,out,table,selected,hashlib.sha256(canonical(plan)).hexdigest(),frozenset());mark('end');print(result,flush=True)
