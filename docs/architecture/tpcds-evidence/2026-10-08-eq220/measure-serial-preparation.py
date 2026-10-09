import hashlib,json,subprocess,statistics
from pathlib import Path
repo=Path.cwd();base=Path('/data2/supabricks-eq/eq220');source=Path('/data2/supabricks-eq/sf100/load-01/state')
config=next((source/'analytics/apply-workers').glob('*/input.json'));reports=[]
for pair in range(1,4):
 for kind in (['j','l'] if pair%2 else ['l','j']):
  release=repo/f'build/eq220/programs/releases/v0.1.0-alpha.36.eq220{kind}'
  label=f'serial-preparation-{kind}-{pair}';output=base/(label+'.json')
  command=['docker','run','--rm','--name','eq220-'+label,'--network','none','--user','1000:1000','--cpuset-cpus','0-7','--memory','16g','--memory-swap','16g','-v',f'{repo}:{repo}:ro','-v',f'{base}:{base}','-v',f'{source}:{source}:ro','-w',str(repo),'sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec',str(release/'python/analytics/python'),'e2e/tpcds/preparation_probe.py','--release',str(release),'--config',str(config),'--root',str(base/'plan-root'),'--spool',str(base/'plan-spool-02/spool.sqlite3'),'--serial','--output',str(output)]
  with (repo/f'build/eq220/{label}.log').open('x') as stream:subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
  receipt=json.loads(output.read_text());reports.append(dict(pair=pair,kind=kind,command=command,receipt=receipt))
  print(label,receipt['seconds'],flush=True)
  (repo/'build/eq220/serial-preparation-measurements.json').write_text(json.dumps(reports,indent=2)+'\n')
hashes={r['receipt']['plan_sha256'] for r in reports};assert len(hashes)==1,hashes
medians={k:statistics.median(t for r in reports if r['kind']==k for t in r['receipt']['seconds']) for k in ['j','l']}
summary=dict(scope='Matched read-only verification and planning; excludes journal read, initialization, mutation and publication',plan_sha256=next(iter(hashes)),medians_seconds=medians,speedup=medians['j']/medians['l'],raw_sha256=hashlib.sha256((repo/'build/eq220/serial-preparation-measurements.json').read_bytes()).hexdigest())
(repo/'build/eq220/serial-preparation-summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
