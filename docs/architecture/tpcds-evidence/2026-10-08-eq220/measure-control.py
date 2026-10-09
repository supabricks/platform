import hashlib,json,statistics,subprocess
from pathlib import Path
repo=Path.cwd();base=Path('/data2/supabricks-eq/eq220');source=Path('/data2/supabricks-eq/sf100/load-01/state/capture/74434562-5f54-4e60-8006-a2619faa6841/control.json');reports=[]
for pair in range(1,4):
 for kind in (['h','i'] if pair%2 else ['i','h']):
  release=repo/f'build/eq220/programs/releases/v0.1.0-alpha.36.eq220{kind}';label=f'control-{kind}-{pair}'
  command=['docker','run','--rm','--name','eq220-'+label,'--network','none','--user','1000:1000','--cpuset-cpus','0-7','--memory','16g','--memory-swap','16g','-v',f'{repo}:{repo}:ro','-v',f'{base}:{base}','-v',f'{source}:{source}:ro','-w',str(repo),'sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec',str(release/'python/analytics/python'),'build/eq220/profile-control.py',str(release),str(source),str(base/label)]
  with (repo/f'build/eq220/{label}.log').open('x') as stream:subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
  receipt=json.loads((base/label/'result.json').read_text());reports.append(dict(pair=pair,kind=kind,command=command,receipt=receipt));print(label,receipt['samples_seconds'],flush=True)
  (repo/'build/eq220/control-measurements.json').write_text(json.dumps(reports,indent=2)+'\n')
hashes={r['receipt']['control_sha256'] for r in reports};assert len(hashes)==1
assert all(r['receipt']['exact_control'] for r in reports)
medians={k:statistics.median(t for r in reports if r['kind']==k for t in r['receipt']['samples_seconds']) for k in ['h','i']}
summary=dict(scope='Isolated unchanged control-file polling; no source/Delta/publication throughput claim',control_sha256=next(iter(hashes)),medians_seconds=medians,speedup=medians['h']/medians['i'],raw_sha256=hashlib.sha256((repo/'build/eq220/control-measurements.json').read_bytes()).hexdigest())
(repo/'build/eq220/control-summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
