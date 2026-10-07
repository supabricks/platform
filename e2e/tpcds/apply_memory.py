"""Sample owned apply workers while the existing descendant supervisor runs."""
import json,subprocess,sys,time
from pathlib import Path
def is_apply_worker(process,confirmed):
    # Exec preserves PID and birth time. A negative role observed between fork
    # and exec must be checked again; otherwise an entire worker is invisible.
    key=(process.pid,process.create_time())
    if key not in confirmed:
        if not any(Path(arg).name=='incremental_worker.py' for arg in process.cmdline()):return False
        confirmed.add(key)
    return True


def main():
 import psutil
 output=Path(sys.argv[1]);command=sys.argv[2:]
 start=time.monotonic();child=subprocess.Popen(command);roles=set();workers={};errors=[];owner=psutil.Process(child.pid)
 with (output/'apply-memory.samples.jsonl').open('x') as stream:
  while True:
   try:children=owner.children(recursive=True) if owner.is_running() else []
   except psutil.NoSuchProcess:children=[]
   for process in children:
    try:
     key=(process.pid,process.create_time())
     if not is_apply_worker(process,roles):continue
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


if __name__=='__main__':main()
