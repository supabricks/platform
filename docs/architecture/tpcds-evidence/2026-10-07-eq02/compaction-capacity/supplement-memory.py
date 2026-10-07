"""Supplement a running qualification without changing its supervisor or limits."""
import json,subprocess,time
from pathlib import Path
import psutil
root=Path('/data2/supabricks-eq/eq03-append/load-10')
identity=json.loads(subprocess.check_output(['docker','inspect','eq189-sf1'],text=True))[0]
owner=psutil.Process(identity['State']['Pid']);created=owner.create_time();started=time.monotonic()
initial=json.loads((root/'load/result.json').read_text())
workers={};roles=set();errors=[];me=psutil.Process();cpu0=sum(me.cpu_times()[:2])
with (root/'supplement-memory.samples.jsonl').open('x') as stream:
 while time.monotonic()-started<7500:
  try:
   if not owner.is_running() or owner.create_time()!=created:break
   children=owner.children(recursive=True)
  except psutil.NoSuchProcess:break
  for p in children:
   try:
    key=(p.pid,p.create_time())
    # Only cache positive roles: a forked daemon child may exec Python later.
    if key not in roles:
     if not any(Path(x).name=='incremental_worker.py' for x in p.cmdline()):continue
     roles.add(key)
    rss=p.memory_info().rss
    fields=dict(line.split(':',1) for line in Path(f'/proc/{p.pid}/status').read_text().splitlines() if ':' in line)
    hwm=int(fields['VmHWM'].split()[0])*1024;seconds=time.monotonic()-started
    d=workers.setdefault(key,dict(pid=p.pid,created_at=key[1],first_observed_seconds=seconds,samples=0,sampled_rss_peak=0,kernel_hwm_peak=0))
    d.update(last_observed_seconds=seconds,samples=d['samples']+1,sampled_rss_peak=max(d['sampled_rss_peak'],rss),kernel_hwm_peak=max(d['kernel_hwm_peak'],hwm))
    stream.write(json.dumps(dict(seconds=seconds,pid=p.pid,created_at=key[1],rss=rss,highwater=hwm))+'\n')
   except (psutil.NoSuchProcess,FileNotFoundError,ProcessLookupError):pass
   except Exception as error:errors.append(dict(seconds=time.monotonic()-started,error=type(error).__name__))
  stream.flush();time.sleep(.1)
(root/'supplement-memory.json').write_text(json.dumps(dict(scope='Supplemental host-namespace read-only observer added during attempt10 after discovery of fork/exec negative-role cache bug in original sampler; original trial and supervisor untouched. Partial lifetime coverage, short-lived workers/final peaks can be missed. No production limit change.',initial_load_elapsed_seconds=initial['elapsed_seconds'],initial_committed_rows=initial['committed_rows'],owner_host_pid=owner.pid,owner_created_at=created,interval_seconds=.1,elapsed_seconds=time.monotonic()-started,observer_cpu_seconds=sum(me.cpu_times()[:2])-cpu0,workers=list(workers.values()),sampling_errors=errors),indent=2)+'\n')
