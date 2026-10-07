import importlib.util,json,resource,sys,time,shutil
from pathlib import Path
sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
import incremental_worker as w
import incremental.maintenance as m
spec=importlib.util.spec_from_file_location('capacity','/repo/e2e/tpcds/capacity.py');cap=importlib.util.module_from_spec(spec);spec.loader.exec_module(cap)
root=Path('/diag');(root/'roots').mkdir();shutil.copytree('/source/roots/baseline',root/'roots/baseline');(root/'work').mkdir()
for f in ['previous.json','measurements.json']:shutil.copyfile('/source/'+f,root/f)
start=time.monotonic()
def mark(name):print(json.dumps(dict(phase=name,seconds=time.monotonic()-start,hwm=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)),flush=True)
def wrap(mod,name):
 old=getattr(mod,name)
 def call(*a,**kw):
  mark(name+':start')
  try:return old(*a,**kw)
  finally:mark(name+':end')
 setattr(mod,name,call)
for mod,names in [(w,['initialize','plan','apply_table','inventory','durable']),(m,['compact','write_deltalake'])]:
 for name in names:wrap(mod,name)
mark('start');cap.run(root,'apply')
