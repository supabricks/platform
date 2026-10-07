import json,sys,time,traceback,resource
from pathlib import Path
sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
import incremental.maintenance as m
root=Path('/failed/load/state');c=json.loads((root/'analytics/apply-work/98819894-44d6-4443-b62b-623261479256/input.json').read_text())
c['previous_generation']=c['previous_generation'].replace('/reports/load/state',str(root))
c['deadline_ms']=int(time.time()*1000)+300000
out=Path('/diag/compacted');out.mkdir();start=time.monotonic()
try:
 m.compact(c,out)
 print('PASS',flush=True)
finally:print('elapsed_seconds',time.monotonic()-start,'hwm_bytes',resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,flush=True)
