"""Frozen #157 sustained qualification, then separate #156 and #157 paired measurements."""
import concurrent.futures,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parent
config_path=root/'qualification-config-01.json';config=json.loads(config_path.read_text())
harness=Path(config['harness']);sys.path.insert(0,str(harness/'e2e/native/performance'))
from compare import harness_identity,package_identity
from host_monitor import HostMonitor
from matrix import affinity,topology

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):
 t=p.with_suffix('.tmp');t.write_text(json.dumps(v,indent=2)+'\n');t.replace(p)
output=root/'qualification-01';output.mkdir(exist_ok=False)
state=dict(status='starting',pid=os.getpid(),completed=[],config_sha256=sha(config_path),script_sha256=sha(Path(__file__)),harness=harness_identity(harness))
monitor=HostMonitor(output,publish_quiet=True).start()
def checkpoint(**values):
 state.update(values,heartbeat_ms=time.time()*1000);save(output/'status.json',state)
def verify():
 monitor.check();assert sha(config_path)==state['config_sha256'] and sha(Path(__file__))==state['script_sha256']
 assert harness_identity(harness)==state['harness'] and shutil.disk_usage(output).free>64*1024**3
 for arm in config['arms'].values():assert package_identity(Path(arm['release']),arm['revision'])==arm['identity']
def execute(name,command):
 verify();checkpoint(status='running',phase=name)
 with (output/(name+'.log')).open('x') as log:
  child=subprocess.Popen(command,cwd=harness,stdout=log,stderr=subprocess.STDOUT);checkpoint(child_pid=child.pid)
  while True:
   try:code=child.wait(timeout=10);break
   except subprocess.TimeoutExpired:checkpoint()
 checkpoint(child_pid=None);return code
try:
 checkpoint(status='waiting_for_quiet',phase='sustained')
 with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
  pending=pool.submit(monitor.wait_quiet)
  while True:
   try:quiet=pending.result(timeout=10);break
   except concurrent.futures.TimeoutError:checkpoint()
 folder=output/'sustained';folder.mkdir();candidate=config['arms']['candidate']
 command=['docker','run','--rm','--init','--name','sp10c-maintenance-sustained-'+str(os.getpid()),'--network','none','--cpuset-cpus',','.join(map(str,affinity(topology(),8))),'--memory','16g','--memory-swap','16g','--user',f'{os.getuid()}:{os.getgid()}',
 '-v',str(harness)+':/repo:ro','-v',candidate['release']+':/release:ro','-v',str(folder)+':/reports','-w','/repo',config['image'],'python3','install/native/catalog_gate.py','--timeout','2400','--report','/reports/cleanup.json','--','python3','e2e/native/performance/sustained_journal.py','--release','/release','--scratch','/reports','--report','/reports/result.json']
 started=time.time()*1000
 try:code=execute('sustained',command)
 finally:
  ended=time.time()*1000;receipt=dict(started_at_ms=started,ended_at_ms=ended,quiet=quiet,overlaps=monitor.overlap(started,ended),sha256={p.name:sha(p) for p in folder.iterdir() if p.is_file()});save(folder/'receipt.json',receipt)
 assert code==0,('sustained',code)
 report=json.loads((folder/'result.json').read_text());cleanup=json.loads((folder/'cleanup.json').read_text())
 assert not receipt['overlaps'],'host contention; preserve and review'
 assert cleanup['exit_code']==cleanup['leaked_descendants']==cleanup['remaining_descendants']==0 and not cleanup['timed_out']
 assert report['status']=='measured' and report['offered_load_met'] and report['within_5s_p95']
 assert json.loads((folder/'reopen.json').read_text())
 history=json.loads((folder/'history.json').read_text());assert history['foreign_keys']=='passed'
 counts=history['counts']
 assert counts['published']>1024 and counts['incremental_runs']<1024 and counts['incremental_requests']<4096 and counts['sync_runs']<10000,counts
 save(folder/'history-check.json',dict(status='passed',counts=counts));state['completed'].append('sustained');checkpoint()
 for phase,old,new,hypothesis in [
  ('history-comparison','original','history','Bounded private execution history removes the admission cliff without a short-workload performance regression'),
  ('maintenance-comparison','history','candidate','Bounded published-history reclamation prevents late capture backpressure without changing durability')]:
  command=[sys.executable,str(harness/'e2e/native/performance/compare.py'),'--slice','SP10c-'+phase,'--hypothesis',hypothesis,'--output',str(output/phase),'--cells','8:1250,16:1250','--clients','8','--seconds','300','--warmup-seconds','60','--repeats','3','--quiet-seconds','300','--minimum-free-gib','64','--image',config['image'],'--host-continuity',str(monitor.quiet_path)]
  for label,which in [('predecessor',old),('candidate',new)]:
   arm=config['arms'][which]
   command+=['--'+label+'-release',arm['release'],'--'+label+'-revision',arm['revision'],'--'+label+'-harness',str(harness)]
  code=execute(phase,command)
  archive=[sys.executable,str(harness/'e2e/native/performance/archive_comparison.py'),str(output/phase),str(output/(phase+'-archive'))]
  if (output/phase/'experiment.json').exists():subprocess.run(archive+(['--allow-incomplete'] if code else []),check=True)
  assert code==0,(phase,code)
  state['completed'].append(phase);checkpoint()
 checkpoint(status='measurements_complete_review_required',phase='review')
except BaseException as e:checkpoint(status='stopped_for_investigation',error_type=type(e).__name__,error=str(e));raise
finally:
 try:monitor.close()
 except BaseException as e:checkpoint(status='stopped_for_investigation',error_type=type(e).__name__,error='host monitor close failed');raise
