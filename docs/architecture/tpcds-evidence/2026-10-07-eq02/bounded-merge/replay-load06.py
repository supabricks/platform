import json,sys,time,shutil,traceback
from pathlib import Path
sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
import incremental_worker as w
source=Path('/failed/load/state');root=Path('/diag/generations/current');root.parent.mkdir(parents=True)
shutil.copytree(source/'analytics/incremental/bffac559-ed15-4724-9795-0a0760c4059a',root)
plan=json.loads((source/'analytics/apply-work/c19162e3-5574-40f6-a9e7-f134e6df54c2/plan.json').read_text())
config=dict(id=plan['run_id'],deadline_ms=int(time.time()*1000)+60000)
for planned in plan['tables']:
 table=dict(path='tables/'+planned['oid']);print('APPLY',planned['oid'],'before',planned['before'],'rows',len(planned['rows']),flush=True)
 print(w.apply_table(config,root,table,planned,w.hashlib.sha256(w.canonical(plan)).hexdigest(),frozenset()),flush=True)
