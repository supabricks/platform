import hashlib,json,subprocess,statistics
from pathlib import Path
repo=Path.cwd();base=Path('/data2/supabricks-eq/eq220');source=Path('/data2/supabricks-eq/sf100/load-01/state');reports=[]
for pair in range(1,4):
    for kind in (['g','h'] if pair%2 else ['h','g']):
        release=repo/f'build/eq220/programs/releases/v0.1.0-alpha.36.eq220{kind}';label=f'wire-{kind}-{pair}'
        command=['docker','run','--rm','--name','eq220-'+label,'--network','none','--user','1000:1000','--cpuset-cpus','0-7','--memory','16g','--memory-swap','16g','-v',f'{repo}:{repo}:ro','-v',f'{base}:{base}','-v',f'{source}:{source}:ro','-w',str(repo),'sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec',str(release/'python/analytics/python'),'build/eq220/profile-wire.py',str(release),str(source),str(base/label)]
        with (repo/f'build/eq220/{label}.log').open('x') as stream:subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
        receipt=json.loads((base/label/'result.json').read_text());reports.append(dict(pair=pair,kind=kind,command=command,receipt=receipt))
        print(label,receipt['unprofiled_seconds'],flush=True)
        (repo/'build/eq220/wire-measurements.json').write_text(json.dumps(reports,indent=2)+'\n')
hashes={r['receipt']['payload_sha256'] for r in reports};assert len(hashes)==1
assert all(r['receipt']['exact_encoded_transactions'] for r in reports)
medians={k:statistics.median(t for r in reports if r['kind']==k for t in r['receipt']['unprofiled_seconds']) for k in ['g','h']}
summary=dict(scope='Isolated matched socketpair transport and strict capture decoding; PostgreSQL/durability/publication excluded',payload_sha256=next(iter(hashes)),medians_seconds=medians,speedup=medians['g']/medians['h'],raw_sha256=hashlib.sha256((repo/'build/eq220/wire-measurements.json').read_bytes()).hexdigest())
(repo/'build/eq220/wire-summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary),flush=True)
