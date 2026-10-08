"""Sample owned apply workers while the existing descendant supervisor runs."""
import json,subprocess,sys,time
from pathlib import Path
import psutil
output=Path(sys.argv[1]);command=sys.argv[2:]
start=time.monotonic();child=subprocess.Popen(command);roles={};workers={};errors=[]
with (output/'apply-memory.samples.jsonl').open('x') as stream:
 while True:
  try:children=psutil.Process(child.pid).children(recursive=True)
  except psutil.NoSuchProcess:children=[]
  for process in children:
   try:
    key=(process.pid,process.create_time())
    if key not in roles:roles[key]=any(Path(arg).name=='incremental_worker.py' for arg in process.cmdline())
    if not roles[key]:continue
    rss=process.memory_info().rss
    values={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path(f'/proc/{process.pid}/status').read_text().splitlines() if ':' in line}
    hwm=int(values.get('VmHWM','0 kB').split()[0])*1024
    now=time.monotonic()-start
    item=workers.setdefault(key,dict(pid=process.pid,created_at=key[1],first_observed_seconds=now,samples=0,sampled_rss_peak=0,kernel_hwm_peak=0))
    item.update(last_observed_seconds=now,samples=item['samples']+1,sampled_rss_peak=max(item['sampled_rss_peak'],rss),kernel_hwm_peak=max(item['kernel_hwm_peak'],hwm))
    stream.write(json.dumps(dict(seconds=now,pid=process.pid,created_at=key[1],rss=rss,highwater=hwm))+'\n')
   except (psutil.NoSuchProcess,FileNotFoundError,ProcessLookupError):pass
   except Exception as error:errors.append(dict(seconds=time.monotonic()-start,error=type(error).__name__))
  stream.flush()
  if child.poll() is not None:break
  time.sleep(.1)
report=dict(scope='100ms external samples of supervisor-owned incremental workers; short-lived workers or final peaks may be missed; no enforced limit change',interval_seconds=.1,exit_code=child.wait(),elapsed_seconds=time.monotonic()-start,workers=list(workers.values()),sampling_errors=errors)
(output/'apply-memory.json').write_text(json.dumps(report,indent=2)+'\n')
raise SystemExit(report['exit_code'])
