from pathlib import Path
import hashlib,json,statistics,sys
root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parent
sys.path.insert(0,str(root/'harness-02/e2e/native/performance'))
from compare import expected
from comparison import load_trial
campaign=root/'campaign-02'
result={'status':'reviewed_keep_worker_reuse','scope':'Three fresh matched pairs per cell; no population confidence or universal capacity claim. Stage percentiles are separate distributions, not additive causal shares.','phases':{}}
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def describe(values):return {'median':statistics.median(values),'min':min(values),'max':max(values),'individual':values}
phases=[campaign/name/'experiment.json' for name in ('common-refactor-controls','common-profiler-controls','historical-main')] + [root/'continuation-01'/name/'experiment.json' for name in ('qualified-main','historical-profiler-controls','qualified-profiler-controls')]
for phase in phases:
 record=json.loads(phase.read_text());assert record['state']=='complete'
 out={'accepted_pairs':len(record['pairs']),'attempts':len(record['attempts']),'trials':[],'cells':[]}
 pairs=[]
 for attempt in record['attempts']:
  arms={}
  for arm,receipt in attempt['results'].items():
   folder=phase.parent/receipt['directory']
   for name,digest in receipt['evidence_sha256'].items():
    path=(folder/name).resolve();assert path.is_relative_to(folder.resolve());assert sha(path)==digest
   metrics=load_trial(folder,expected(record['config'],arm,attempt['pair']));assert metrics==receipt['metrics']
   trial=json.loads(next(folder.glob('*-cpu*/trial.json')).read_text())
   values={k:metrics.get(k) for k in ['lag_p95_ms','lag_p99_ms','source_rows_s','cpu_cores','peak_memory_bytes','successful_apply_workers']}
   values.update({key+'_p95_ms':v['p95'] for key,v in trial.get('stages_ms',{}).items()})
   row={'directory':receipt['directory'],'arm':arm,'accepted':attempt['accepted'],'cpus':attempt['pair']['cpus'],'rate':attempt['pair']['rate'],'repeat':attempt['pair']['repeat'],'status':metrics['status'],'fresh':metrics.get('within_5s_p95'),'input_met':metrics.get('offered_load_met'),'values':values}
   arms[arm]=row;out['trials'].append(row)
  if attempt['accepted']:pairs.append((attempt['pair'],arms))
 assert len(pairs)==len(record['pairs'])
 for cpu,rate in sorted({(p['cpus'],p['rate']) for p,a in pairs}):
  selected=[a for p,a in pairs if (p['cpus'],p['rate'])==(cpu,rate)]
  cell={'cpus':cpu,'rate':rate,'pairs':len(selected),'arms':{},'paired_percent':{}}
  for arm in ['predecessor','candidate']:
   rows=[a[arm] for a in selected];cell['arms'][arm]={'fresh':sum(r['fresh'] is True for r in rows),'input_met':sum(r['input_met'] is True for r in rows),'measured':sum(r['status']=='measured' for r in rows),'metrics':{k:describe([r['values'][k] for r in rows]) for k in rows[0]['values'] if all(r['values'].get(k) is not None for r in rows)}}
  keys=set(cell['arms']['predecessor']['metrics'])&set(cell['arms']['candidate']['metrics'])
  for k in sorted(keys):
   if all(a['predecessor']['values'][k]>0 for a in selected):cell['paired_percent'][k]=describe([100*(a['candidate']['values'][k]/a['predecessor']['values'][k]-1) for a in selected])
  out['cells'].append(cell)
 result['phases'][phase.parent.name]=out
 print(phase.parent.name,len(out['trials']),'verified',flush=True)
receipts=json.loads((campaign/'component/receipts.json').read_text());rows=[]
for receipt in receipts:
 folder=campaign/'component'/receipt['directory']
 for name,digest in receipt['sha256'].items():assert sha(folder/name)==digest
 d=json.loads((folder/'component.json').read_text());assert d['status']=='PASS'
 cleanup=json.loads((folder/'cleanup.json').read_text());assert cleanup['exit_code']==cleanup['leaked_descendants']==cleanup['remaining_descendants']==0 and not cleanup['timed_out']
 m=d['metrics']['dispatch_component'];rows.append({'arm':receipt['arm'],'repeat':receipt['repeat'],'accepted':receipt['accepted'],'idle_cpu_cores':m['idle_cpu_cores'],'status_p95_ms':m['status_latency_ms']['p95'],'status_p99_ms':m['status_latency_ms']['p99'],'lag_p95_ms':d['metrics']['workload']['commit_to_publication_ms']['p95'],'checks':d['checks']})
result['component']={'trials':rows,'medians':{arm:{key:statistics.median(r[key] for r in rows if r['arm']==arm) for key in ['idle_cpu_cores','status_p95_ms','status_p99_ms','lag_p95_ms']} for arm in ['common','candidate']}}
result['review_script_sha256']=sha(Path(__file__))
(root/'review-results.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result['component']['medians']))
for name in ['historical-main','qualified-main','qualified-profiler-controls']:
 for cell in result['phases'][name]['cells']:
  print(name,cell['cpus'],cell['rate'],json.dumps({arm:{k:round(v['median'],4) for k,v in a['metrics'].items() if k in ['lag_p95_ms','source_rows_s','cpu_cores','commit_to_admission_p95_ms','peak_memory_bytes','successful_apply_workers']} for arm,a in cell['arms'].items()}),json.dumps({k:round(v['median'],3) for k,v in cell['paired_percent'].items() if k in ['lag_p95_ms','source_rows_s','cpu_cores','peak_memory_bytes']}))
