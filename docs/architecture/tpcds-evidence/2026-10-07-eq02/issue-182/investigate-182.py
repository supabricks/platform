"""Controlled retained-state diagnostic; no native-worker qualification claim."""
import gc,json,os,resource,shutil,sys,threading,time
from pathlib import Path
import psutil
os.umask(0o077)
sys.path.insert(0,'/repo/build/eq02-20261007/row-prefix-runtime/python/analytics')
import incremental_worker as w
from incremental.storage import journal
from incremental.planning import mutation_lease
mode=sys.argv[1]
r=json.load(open('/evidence/load-04/result.json'))
d=json.load(open('/evidence/load-04/last-publication.json'))['descriptor']
run=json.load(open('/evidence/load-04/last-incremental-runs.json'))[0]
c=dict(id=run['id'],identity=r['final_capture']['identity'],bootstrap_lsn=r['final_capture']['bootstrap_lsn'],after_lsn=run['after_lsn'],target_lsn=run['target_lsn'],deadline_ms=int(time.time()*1000)+120000)
shutil.copytree(Path('/state/capture')/r['final_capture']['id']/'spool','/diag/spool')
c['spool']='/diag/spool/spool.sqlite3'
shutil.copytree(Path('/state')/d['generation'],'/diag/analytics/incremental/generation')
root=Path('/diag/analytics/incremental/generation')
report=dict(mode=mode,status='RUNNING',events=[],guard_checks=0,batches=0,matched_rows=0)
p=psutil.Process();phase='start';start=time.monotonic();stop=threading.Event()
def sample():
 pool=w.pa.default_memory_pool()
 return dict(seconds=time.monotonic()-start,phase=phase,rss=p.memory_info().rss,highwater=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,arrow_bytes=pool.bytes_allocated(),arrow_peak=pool.max_memory(),threads=p.num_threads())
def event(name):
 global phase
 phase=name;report['events'].append(sample())
def monitor():
 with open('/diag/samples.jsonl','x') as f:
  while not stop.wait(.005):f.write(json.dumps(sample())+'\n')
thread=threading.Thread(target=monitor);thread.start()
check=w.PlanningBoundary.check
def guarded(self):
 report['guard_checks']+=1
 return check(self)
w.PlanningBoundary.check=guarded
original_filter=w.key_filter
if mode in ('range','bounded-range'):
 def ranged_filter(columns,keys):
  result=original_filter(columns,keys)
  pk=w.key_columns(columns);parts=[w.key_values(k,pk) for k in keys]
  for n,i in enumerate(pk):
   values=[k[n] for k in parts];field=w.ds.field(columns[i][1])
   result=result & (field>=min(values)) & (field<=max(values))
  return result
 w.key_filter=ranged_filter
scan=w.key_batches
def scanned(dataset,columns,keys):
 event('scan')
 if mode in ('bounded','bounded-release','bounded-large','bounded-range'):
  pk=w.key_columns(columns)
  def bounded():
   for batch in dataset.scanner(filter=w.key_filter(columns,keys),batch_size=1024 if mode=='bounded-large' else 32,batch_readahead=1,fragment_readahead=1,use_threads=False).to_batches():
    if len(pk)>1:
     parts=[batch.column(columns[i][1]).to_pylist() for i in pk]
     batch=batch.filter(w.pa.array([tuple(v) in keys for v in zip(*parts)],type=w.pa.bool_()))
    yield batch
  batches=bounded()
 else:batches=scan(dataset,columns,keys)
 for batch in batches:
  report['batches']+=1;report['matched_rows']+=batch.num_rows
  yield batch
 event('overlay')
w.key_batches=scanned
try:
 event('journal');data=journal(c);event('planning')
 with mutation_lease(root) as lease:plan=w.plan(c,root,d,data,lease)
 event('planned')
 if mode=='bounded-release':
  gc.collect();w.pa.default_memory_pool().release_unused();event('released')
 checksum=w.hashlib.sha256(w.canonical(plan)).hexdigest()
 report['plan_sha256']=checksum
 for table in d['manifest']['tables']:
  planned=next((t for t in plan['tables'] if t['oid']==str(table['oid'])),None)
  if planned:
   event('apply');version,metrics=w.apply_table(c,root,table,planned,checksum,frozenset());event('applied')
   report['metrics']=metrics;report['version']=version
 gc.collect();event('collected')
 report.update(status='PASS',planned_rows=sum(len(t['rows']) for t in plan['tables']),end_lsn=plan['end_lsn'])
except BaseException as e:report.update(status='FAIL',error=repr(e));raise
finally:
 stop.set();thread.join();report['elapsed_seconds']=time.monotonic()-start
 Path('/diag/result.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report))
