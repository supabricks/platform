import json,os,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parent;repo=root.parent.parent
config={'arms':{'candidate':{'release':str(root/'sqlite-owner-runtime-01')}},'image':'sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec'};harness=root.parent/'sp10c-20261003/harness-07';out=root/'screen-01';out.mkdir()
state={'pid':os.getpid(),'status':'starting','completed':[]}
def save(**values):
 state.update(values,heartbeat_ms=time.time()*1000);tmp=out/'status.tmp';tmp.write_text(json.dumps(state,indent=2));tmp.replace(out/'status.json')
steps=[('backend',['/release/python/analytics/python','e2e/native/performance/owner_component.py','--analytics','/release/python/analytics','--scratch','/reports','--report','/reports/result.json']),
 ('lifecycle',['python3','e2e/native/performance/reuse_component.py','--binary','/release/bin/supabricks','--bundle','/release/engine','--helpers','/release/helpers','--python','/release/python/analytics/python','--worker','/release/python/analytics/export.py','--report','/reports/result.json'])]
for mode in ['off','on']:steps.append(('observer-'+mode,['python3','e2e/native/performance/backend_observer_control.py','--release','/release','--scratch','/reports','--report','/reports/result.json','--observer',mode,'--seconds','5','--warmup','5']))
try:
 for name,command in steps:
  folder=out/name;folder.mkdir();save(status='running',phase=name)
  cmd=['docker','run','--rm','--init','--name','sp10c-history-screen-'+str(os.getpid()),'--network','none','--cpuset-cpus','0-3,8-11','--memory','16g','--memory-swap','16g','--user',f'{os.getuid()}:{os.getgid()}',
   '-v',str(harness)+':/repo:ro','-v',config['arms']['candidate']['release']+':/release:ro','-v',str(folder)+':/reports','-w','/repo',config['image'],'python3','install/native/catalog_gate.py','--timeout','900','--report','/reports/cleanup.json','--',*command]
  with (out/(name+'.log')).open('w') as log:
   child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
   while True:
    try:code=child.wait(timeout=10);break
    except subprocess.TimeoutExpired:save(child_pid=child.pid)
  assert code==0,(name,code)
  cleanup=json.loads((folder/'cleanup.json').read_text());assert cleanup['exit_code']==cleanup['leaked_descendants']==cleanup['remaining_descendants']==0 and not cleanup['timed_out']
  report=json.loads((folder/'result.json').read_text());assert report['status'] in ('PASS','measured')
  state['completed'].append(name);save()
 save(status='passed',phase='complete',child_pid=None)
except BaseException as e:save(status='failed',error=str(e));raise
