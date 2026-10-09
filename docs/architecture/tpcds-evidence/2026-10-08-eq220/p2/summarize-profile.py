"""Join coarse diagnostic stages to the same published-row cohort; nested times are not additive."""
import json,sqlite3,statistics,sys
from pathlib import Path
repo=Path.cwd();sys.path.insert(0,str(repo/'e2e/tpcds'))
from sync_costs import run as batch_costs
root=Path(sys.argv[1]);out=Path(sys.argv[2]);traces=root/'state/sync-profile'
db=sqlite3.connect((root/'state/state.sqlite3').resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
pubs=[]
for published,raw in db.execute("select published_at_ms,descriptor from publications where state='published' order by ordinal"):
 d=json.loads(raw);pubs.append(dict(at_ms=published,rows=sum(t['rows'] for t in d['manifest']['tables'])))
db.close();start=next(p for p in pubs if p['rows']>=6500000);end=next(p for p in pubs if p['rows']>=7300000)
costs=[r for r in batch_costs(root/'state')['batches'] if r['started_ms']>=start['at_ms'] and r['published_ms']<=end['at_ms']];by_id={r['run_id']:r for r in costs}
workers=[]
for path in traces.glob('incremental*.jsonl'):
 rows=[json.loads(line) for line in path.read_text().splitlines()];r=rows[-1]
 if r['context_id'] not in by_id:continue
 assert r['final'] and not r['budget_exceeded'] and r['profile_write_errors']==0
 b=by_id[r['context_id']]
 workers.append(dict(run_id=r['context_id'],batch=b,metrics=r['metrics'],request_cpu_s=r['request_cpu_s'],post_manifest_worker_ms=r['at_ms']-b['manifest_ms'],after_worker_profile_ms=b['published_ms']-r['at_ms']))
assert len(workers)==len(costs)
def metric_medians(rows):
 labels=sorted({k for r in rows for k in r['metrics']})
 return {k:{field:statistics.median(r['metrics'].get(k,{}).get(field,0) for r in rows) for field in ['calls','total_ns','self_ns','max_ns']} for k in labels}
def diff_trace(path):
 rows=[json.loads(line) for line in path.read_text().splitlines()]
 a=max((r for r in rows if r['at_ms']<=start['at_ms']),key=lambda r:r['at_ms']);b=max((r for r in rows if r['at_ms']<=end['at_ms']),key=lambda r:r['at_ms'])
 assert not b['budget_exceeded'] and not b['profile_write_errors']
 metrics={k:{field:v.get(field,0)-a['metrics'].get(k,{}).get(field,0) for field in ['calls','total_ns','self_ns']} for k,v in b['metrics'].items()}
 result=dict(first_at_ms=a['at_ms'],last_at_ms=b['at_ms'],elapsed_ms=b['at_ms']-a['at_ms'],metrics=metrics)
 if b.get('native_io'):
  result['native_io_delta']={name:{k:v-a['native_io'][name][k] for k,v in vals.items() if k in ['calls','total_ns','errors']} for name,vals in b['native_io'].items()}
  result['cpu_seconds']=b['cpu_user_s']+b['cpu_system_s']-a['cpu_user_s']-a['cpu_system_s']
  result['stack_sample_delta']={k:v-a.get('sampled_main_stacks',{}).get(k,0) for k,v in b.get('sampled_main_stacks',{}).items()}
 return result
capture_files=list(traces.glob('capture*.jsonl'));assert len(capture_files)==1
result=dict(scope='Diagnostic package only, profiling overhead present. Worker medians are per complete run within the cohort. Capture/controller snapshots round down independently. Nested phase times and different-thread totals must not be added. Apply gc_collect is not captured in final request snapshots; after-worker interval includes GC and controller work.',start=start,end=end,worker_count=len(workers),worker_metric_medians=metric_medians(workers),worker_interval_medians={k:statistics.median(r[k] for r in workers) for k in ['request_cpu_s','post_manifest_worker_ms','after_worker_profile_ms']},workers=workers,capture=diff_trace(capture_files[0]),controller=diff_trace(traces/'daemon.jsonl'))
with out.open('x') as stream:json.dump(result,stream,indent=2);stream.write('\n')
