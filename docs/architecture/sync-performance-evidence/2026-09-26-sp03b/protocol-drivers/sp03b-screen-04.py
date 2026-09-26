import sys,json,time,subprocess,os,random,hashlib
from pathlib import Path
repo=Path('/tmp/sp03b-harness-04');sys.path.insert(0,str(repo/'e2e/native/performance'))
from host_monitor import HostMonitor
out=Path('/tmp/sp03b-component-04');out.mkdir();monitor=HostMonitor(out).start()
configs=[dict(journal_mode=m,rate=r,single=s,profile=True) for m in ('delete','wal') for r in (0,500) for s in (False,True)]
trials=[dict(config=c,repeat=r,kind='factorial') for c in configs for r in (1,2,3)];random.Random(20260926).shuffle(trials)
for repeat in (1,2,3):
 for enabled in ((True,False) if repeat%2 else (False,True)):
  trials.append(dict(config=dict(journal_mode='wal',rate=0,single=False,profile=enabled),repeat=repeat,kind='observer-control'))
manifest=dict(state='running',protocol='SP03b predeclared 24 factorial + 6 observer-control trials; 10s FULL, bounded readers and pruning',revision='5382e80032bb4fc70706f112db26f8415849a961',harness_revision='5382e80032bb4fc70706f112db26f8415849a961',trials=[],seed=20260926)
def save():(out/'screen.json').write_text(json.dumps(manifest,indent=2)+'\n')
save()
try:
 for i,trial in enumerate(trials,1):
  c=trial['config'];print('WAIT_QUIET',i,c,flush=True)
  for attempt in range(1,4):
   quiet=monitor.wait_quiet();start=time.time()*1000;dest=out/f'{i:02}-attempt{attempt}.json'
   cmd=['/tmp/sp03b-runtime-03/python/analytics/python','-B',str(repo/'e2e/native/performance/capture_groups.py'),'--analytics','/tmp/sp03b-runtime-03/python/analytics','--scratch','/tmp','--seconds','10','--rate',str(c['rate']),'--journal-mode',c['journal_mode'],'--reader','--output',str(dest)]
   if c['single']:cmd+=['--single']
   env=dict(os.environ)
   if c['profile']:env['LD_PRELOAD']='/tmp/sp03b-runtime-03/python/analytics/profile_io.so'
   else:cmd+=['--no-profile'];env.pop('LD_PRELOAD',None)
   result=subprocess.run(cmd,env=env,capture_output=True,text=True)
   (out/f'{i:02}-attempt{attempt}.private.log').write_text(result.stdout+result.stderr)
   time.sleep(5);end=time.time()*1000;events=monitor.overlap(start,end)
   receipt=dict(trial=trial,attempt=attempt,quiet=quiet,start_ms=start,end_ms=end,overlap=events,file=dest.name,exit_code=result.returncode)
   manifest['trials'].append(receipt);save()
   if result.returncode:raise RuntimeError('component trial failed: '+dest.name)
   print('COMPLETE',i,'attempt',attempt,'contended',bool(events),flush=True)
   if not events:break
  else:raise RuntimeError('component retry budget exhausted')
 manifest['state']='complete';save()
finally:monitor.close()
