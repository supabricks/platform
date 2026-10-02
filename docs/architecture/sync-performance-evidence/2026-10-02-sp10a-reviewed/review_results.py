from pathlib import Path
import hashlib,json,statistics,sys
root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parent
sys.path.insert(0,str(root/'harness-01/e2e/native/performance'))
from compare import expected
from comparison import load_trial
campaign=root/'campaign-01'
result={'status':'main_comparisons_verified_pending_fixture_review','scope':'Three fresh matched pairs per cell; no population confidence or universal capacity claim. Stage percentiles are separate distributions, not additive causal shares.','phases':{}}
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def describe(values):return {'median':statistics.median(values),'min':min(values),'max':max(values),'individual':values}
phases=[campaign/name/'experiment.json' for name in ('historical-main','qualified-main','historical-profiler-controls','qualified-profiler-controls')]
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
fixtures={}
for name,count in [('backend-component',6),('lifecycle',6),('predecessor-observer-controls',12),('candidate-observer-controls',12)]:
 receipts=json.loads((campaign/name/'receipts.json').read_text());rows=[]
 assert sum(r.get('accepted',False) for r in receipts)==count
 for r in receipts:
  folder=campaign/name/r['directory']
  for file,digest in r['sha256'].items():assert sha(folder/file)==digest
  cleanup=json.loads((folder/'cleanup.json').read_text());assert cleanup['exit_code']==cleanup['leaked_descendants']==cleanup['remaining_descendants']==0 and not cleanup['timed_out']
  d=json.loads((folder/'result.json').read_text());assert d['status'] in ('PASS','measured')
  if r['accepted']:assert not r['overlaps'] and r['quiet']['quiet_seconds']>=300
  row=dict(directory=r['directory'],accepted=r['accepted'],arm=r['step']['arm'],cpus=r['step']['cpus'])
  if name=='backend-component':
   assert d['prune_cycles']>=3 and d['transactions']==4096
   row['metrics']={k:d[k] for k in ['transactions_per_second','cpu_seconds','peak_physical_bytes']}
  elif name=='lifecycle':
   assert all(c['status']=='PASS' for c in d['checks'])
   m=d['metrics']['dispatch_component'];row['metrics']=dict(idle_cpu_cores=m['idle_cpu_cores'],status_p95_ms=m['status_latency_ms']['p95'])
  else:
   assert d['offered_load_met'] and 'both_published_tables_equal_frozen_postgres_source' in d['checks']
   row['observer']=d['observer'];row['metrics']=dict(source_rows_s=d['source']['achieved_rows_per_second'],cpu_cores=d['cpu']['average_cpu_cores'],drain_seconds=d['drain_seconds'])
  rows.append(row)
 groups={}
 for row in rows:
  if row['accepted']:groups.setdefault(str(row['cpus'])+'-'+row.get('observer',row['arm']),[]).append(row)
 summary={k:{m:describe([r['metrics'][m] for r in v]) for m in v[0]['metrics']} for k,v in groups.items()}
 pairs=[]
 for index in range(0,len(rows),2):
  a,b=rows[index:index+2]
  if not a['accepted']:continue
  key='observer' if 'observer' in a else 'arm'
  left,right=('off','on') if key=='observer' else ('predecessor','candidate')
  arms={a[key]:a,b[key]:b};pairs.append(dict(cpus=a['cpus'],percent={m:100*(arms[right]['metrics'][m]/arms[left]['metrics'][m]-1) for m in arms[left]['metrics'] if arms[left]['metrics'][m]>0}))
 fixtures[name]=dict(trials=rows,summary=summary,paired_percent={str(cpu):{m:describe([p['percent'][m] for p in pairs if p['cpus']==cpu]) for m in pairs[0]['percent']} for cpu in {p['cpus'] for p in pairs}})
 print(name,json.dumps({k:{m:round(v['median'],4) for m,v in s.items()} for k,s in summary.items()}),json.dumps({k:{m:round(v['median'],3) for m,v in s.items()} for k,s in fixtures[name]['paired_percent'].items()}))
result['fixtures']=fixtures
assert json.loads((campaign/'status.json').read_text())['status']=='measurements_complete_review_required'
result['status']='reviewed_keep_sqlite_backend_contract'
result['review_script_sha256']=sha(Path(__file__))
(root/'review-results.json').write_text(json.dumps(result,indent=2)+'\n')

for name in ['historical-main','qualified-main','qualified-profiler-controls']:
 for cell in result['phases'][name]['cells']:
  print(name,cell['cpus'],cell['rate'],json.dumps({arm:{k:round(v['median'],4) for k,v in a['metrics'].items() if k in ['lag_p95_ms','source_rows_s','cpu_cores','commit_to_admission_p95_ms','peak_memory_bytes','successful_apply_workers']} for arm,a in cell['arms'].items()}),json.dumps({k:round(v['median'],3) for k,v in cell['paired_percent'].items() if k in ['lag_p95_ms','source_rows_s','cpu_cores','peak_memory_bytes']}))
