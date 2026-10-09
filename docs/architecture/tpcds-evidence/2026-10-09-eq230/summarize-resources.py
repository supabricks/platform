"""Read-only sampled cgroup/role costs, reporting actual observed cohort endpoints."""
import datetime,json,sys
from pathlib import Path

def at(value):return datetime.datetime.fromisoformat(value).timestamp()
def cpu(sample):return {k:int(v) for k,v in (line.split() for line in sample.get('cpu.stat','').splitlines())}
def io(sample):
 out={}
 for line in sample.get('io.stat','').splitlines():
  device,*values=line.split();out[device]={k:int(v) for k,v in (p.split('=',1) for p in values)}
 return out

def summary(control,lower=6500000,upper=7300000):
 all_samples=[json.loads(line) for line in (control/'resources.jsonl').read_text().splitlines()]
 samples=[s for s in all_samples if lower<=(s.get('published_rows') or 0)<=upper]
 assert len(samples)>=2
 first,last=samples[0],samples[-1];elapsed=at(last['utc'])-at(first['utc'])
 cpus={k:v-cpu(first).get(k,0) for k,v in cpu(last).items()}
 io_before=io(first);io_delta={device:{k:v-io_before.get(device,{}).get(k,0) for k,v in values.items()} for device,values in io(last).items()}
 roles={};peaks={};previous={};missing=0
 for sample in samples:
  current={}
  for p in sample.get('processes',[]):
   key=(p['pid'],p['start_ticks']);current[key]=p
   peaks[p['role']]=max(peaks.get(p['role'],0),p['rss_bytes'])
   if key in previous:
    delta=p['user_ticks']+p['system_ticks']-previous[key]['user_ticks']-previous[key]['system_ticks']
    assert delta>=0;roles[p['role']]=roles.get(p['role'],0)+delta/sample['clock_ticks_per_second']
  missing+=len(set(previous)-set(current));previous=current
 return dict(scope='Sampled published-row cohort; cgroup CPU/IO include all cell processes. IO is per device, not summed across device layers. Role counters miss short-lived process tails and sampled RSS is not a hard high-water mark.',requested_rows=[lower,upper],first_published_rows=first['published_rows'],last_published_rows=last['published_rows'],first_utc=first['utc'],last_utc=last['utc'],elapsed_seconds=elapsed,cgroup_cpu_delta=cpus,average_cgroup_cores=cpus['usage_usec']/1e6/elapsed,io_delta_by_device=io_delta,role_cpu_seconds=roles,role_average_cores={k:v/elapsed for k,v in roles.items()},role_peak_sampled_rss_bytes=peaks,disappeared_process_tails=missing,peak_cgroup_memory_bytes=max(int(s.get('memory.current',0)) for s in samples),observed_compilers=[s['observed_compilers'] for s in samples if s.get('observed_compilers')])
if __name__=='__main__':
 result=summary(Path(sys.argv[1]),*(map(int,sys.argv[3:]) if len(sys.argv)>3 else ()))
 with Path(sys.argv[2]).open('x') as stream:json.dump(result,stream,indent=2);stream.write('\n')
