import hashlib,json,subprocess,statistics
from pathlib import Path
repo=Path.cwd();base=Path('/data2/supabricks-eq/eq220');source=Path('/data2/supabricks-eq/sf100/load-01/state')
reports=[]
for pair in range(1,4):
    for kind in (['e','f'] if pair%2 else ['f','e']):
        release=repo/f'build/eq220/programs/releases/v0.1.0-alpha.36.eq220{kind}'
        label=f'plan-{kind}-{pair}'
        command=['docker','run','--rm','--name','eq220-'+label,'--network','none','--user','1000:1000','--cpuset-cpus','0-7','--memory','16g','--memory-swap','16g','-v',f'{repo}:{repo}:ro','-v',f'{base}:{base}','-v',f'{source}:{source}:ro','-w',str(repo),'sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec',str(release/'python/analytics/python'),'build/eq220/profile-plan-v2.py',str(release),str(source),str(base/'plan-root'),str(base/label)]
        with (repo/f'build/eq220/{label}.log').open('x') as stream:subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
        receipt=json.loads((base/label/'result.json').read_text());reports.append(dict(pair=pair,kind=kind,command=command,receipt=receipt))
        print(label,receipt['unprofiled_plan_seconds'],flush=True)
        (repo/'build/eq220/scalar-measurements.json').write_text(json.dumps(reports,indent=2)+'\n')
hashes={h for r in reports for h in r['receipt']['plan_sha256']};assert len(hashes)==1,hashes
medians={k:statistics.median(t for r in reports if r['kind']==k for t in r['receipt']['unprofiled_plan_seconds']) for k in ['e','f']}
summary=dict(scope='Isolated matched planning only; cProfile time excluded from unprofiled samples',plan_sha256=next(iter(hashes)),medians_seconds=medians,speedup=medians['e']/medians['f'],raw_sha256=hashlib.sha256((repo/'build/eq220/scalar-measurements.json').read_bytes()).hexdigest())
(repo/'build/eq220/scalar-summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
