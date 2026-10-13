import json,time,threading,sys,os,shutil
from pathlib import Path
import psutil
sys.path.insert(0,'/repo/build/eq02-20261007/row-prefix-runtime/python/analytics')
import incremental_worker as w
from incremental.storage import journal
from incremental.planning import mutation_lease
r=json.load(open('/evidence/load-04/result.json'))
d=json.load(open('/evidence/load-04/last-publication.json'))['descriptor']
run=json.load(open('/evidence/load-04/last-incremental-runs.json'))[0]
c=dict(id=run['id'],identity=r['final_capture']['identity'],bootstrap_lsn=r['final_capture']['bootstrap_lsn'],
       after_lsn=run['after_lsn'],target_lsn=run['target_lsn'],deadline_ms=int(time.time()*1000)+120000,
       spool='/state/capture/'+r['final_capture']['id']+'/spool/spool.sqlite3')
shutil.copytree(Path(c['spool']).parent,'/diag/spool')
c['spool']='/diag/spool/spool.sqlite3'
root=Path('/state')/d['generation']
report=dict(status='RUNNING',scope='read-only failed-apply planning diagnostic; instrumented, not a performance comparison',
            phases={},samples=[],guard_checks=0,scanned_batches=0,matched_rows=0)
phase='journal';stop=threading.Event();p=psutil.Process();start=time.monotonic()
def monitor():
 with open('/diag/samples.jsonl','x') as f:
  while not stop.wait(.05):
   sample=dict(seconds=time.monotonic()-start,phase=phase,rss=p.memory_info().rss,cpu=p.cpu_times().user+p.cpu_times().system)
   f.write(json.dumps(sample)+'\n');f.flush()
thread=threading.Thread(target=monitor);thread.start()
check=w.PlanningBoundary.check

def guarded(self):
 report['guard_checks']+=1
 return check(self)
w.PlanningBoundary.check=guarded
changes=w.changes

def decoded(*args,**kw):
 global phase
 phase='decode';started=time.monotonic()
 result=changes(*args,**kw)
 report['phases']['decode_seconds']=report['phases'].get('decode_seconds',0)+time.monotonic()-started
 return result
w.changes=decoded
scan=w.key_batches

def scanned(*args,**kw):
 global phase
 phase='scan'
 for batch in scan(*args,**kw):
  report['scanned_batches']+=1;report['matched_rows']+=batch.num_rows
  yield batch
 phase='overlay'
w.key_batches=scanned
try:
 data=journal(c);report['journal_transactions']=len(data[1]);report['journal_seconds']=time.monotonic()-start
 phase='planning'
 with mutation_lease(root) as lease:plan=w.plan(c,root,d,data,lease)
 report.update(status='PASS',planned_rows=sum(len(t['rows']) for t in plan['tables']),end_lsn=plan['end_lsn'])
except BaseException as e:report.update(status='FAIL',error=repr(e));raise
finally:
 stop.set();thread.join();report['elapsed_seconds']=time.monotonic()-start
 Path('/diag/result.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report))
