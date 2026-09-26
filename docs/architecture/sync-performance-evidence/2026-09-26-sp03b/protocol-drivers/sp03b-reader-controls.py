"""Run only after series04 completes; separately declared component controls."""
import hashlib,json,os,subprocess,sys,time
from pathlib import Path
harness=Path('/tmp/sp03b-harness-04');sys.path.insert(0,str(harness/'e2e/native/performance'))
from host_monitor import HostMonitor
out=Path('/tmp/sp03b-reader-controls-01');out.mkdir()
for name in ('predecessor-controls','candidate-controls','comparison','delete-ablation','wal-ablation'):
 assert json.loads(Path('/tmp/sp03b-'+name+'-01/experiment.json').read_text())['state']=='complete'
monitor=HostMonitor(out).start()
record=dict(state='running',protocol_revision='d2b2197b68e6de5f22f8311f8fd3239d02270d29',runtime_revision='5382e80032bb4fc70706f112db26f8415849a961',harness_revision='5382e80032bb4fc70706f112db26f8415849a961',release_sha256=hashlib.sha256(Path('/tmp/sp03b-runtime-03/release.json').read_bytes()).hexdigest(),attempts=[],pairs=[])
def save():(out/'experiment.json').write_text(json.dumps(record,indent=2)+'\n')
save()
try:
 for repeat in (1,2,3):
  for attempt in (1,2,3):
   pair=dict(repeat=repeat,attempt=attempt,results={},accepted=False);record['attempts'].append(pair);save()
   for reader in ((True,False) if repeat%2 else (False,True)):
    quiet=monitor.wait_quiet();start=time.time()*1000;name=f'{repeat}-attempt{attempt}-reader{int(reader)}.json';dest=out/name
    cmd=['/tmp/sp03b-runtime-03/python/analytics/python','-B',str(harness/'e2e/native/performance/capture_groups.py'),'--analytics','/tmp/sp03b-runtime-03/python/analytics','--scratch','/tmp','--seconds','10','--rate','0','--journal-mode','wal','--no-profile','--output',str(dest)]
    if reader:cmd.append('--reader')
    env=dict(os.environ);env.pop('LD_PRELOAD',None)
    result=subprocess.run(cmd,env=env,capture_output=True,text=True);(out/(name+'.private.log')).write_text(result.stdout+result.stderr)
    time.sleep(5);end=time.time()*1000
    pair['results'][str(reader)]=dict(file=name,reader=reader,exit_code=result.returncode,quiet=quiet,start_ms=start,end_ms=end,overlap=monitor.overlap(start,end));save()
    if result.returncode:raise RuntimeError('component observer-control runtime failure: '+name)
   pair['accepted']=not any(r['overlap'] for r in pair['results'].values());save()
   if pair['accepted']:record['pairs'].append(pair);save();print('PAIR_COMPLETE',repeat,flush=True);break
   print('CONTENDED_PAIR_RETAINED',repeat,attempt,flush=True)
  else:raise RuntimeError('observer-control contention retry budget exhausted')
 record['state']='complete';save()
finally:monitor.close()
