import json,sys,time,sqlite3,resource,traceback,shutil
from pathlib import Path
sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
import incremental_worker as w
import incremental.maintenance as m
root=Path('/failed/load/state');out=Path('/diag')
c=sqlite3.connect('file:'+str(root/'state.sqlite3')+'?mode=ro',uri=True)
run=json.loads(c.execute("select record from incremental_runs where id='daf8ae09-0aac-4544-819f-075e8108d4a2'").fetchone()[0])
d=json.loads(c.execute('select descriptor from publications where epoch_id=?',(run['previous_epoch'],)).fetchone()[0])
cap=json.loads(Path('/failed/load/result.json').read_text())['final_capture']
shutil.copytree(root/'capture'/cap['id']/'spool',out/'spool')
config=dict(run,previous=d,identity=cap['identity'],generation=str(out/'generation'),previous_generation=str(root/d['generation']),workspace=str(out/'work'),bootstrap_id=cap['bootstrap_id'],bootstrap_lsn=cap['bootstrap_lsn'],spool=str(out/'spool/spool.sqlite3'),deadline_ms=int(time.time()*1000)+300000,ordinal=d['ordinal']+1)
(out/'work').mkdir(exist_ok=False)
started=time.monotonic()
def mark(name):print(json.dumps(dict(phase=name,seconds=time.monotonic()-started,hwm_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)),flush=True)
def wrap(module,name):
 original=getattr(module,name)
 def call(*args,**kwargs):
  mark(name+':start')
  try:return original(*args,**kwargs)
  finally:mark(name+':end')
 setattr(module,name,call)
for module,names in [(w,['journal','initialize','plan','apply_table','inventory','durable','verify_previous']),(m,['compact'])]:
 for name in names:wrap(module,name)
mark('start')
try:w.run(config);mark('PASS')
except BaseException:traceback.print_exc();mark('FAIL');raise
