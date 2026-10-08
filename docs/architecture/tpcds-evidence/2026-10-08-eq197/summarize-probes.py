import collections,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,'e2e/tpcds')
from compare import disposition
root=Path('/data2/supabricks-eq/eq197/probes');ref=Path('/data2/supabricks-eq/eq02-20261007/reference-02')
expected={q['id']:q for q in json.loads((ref/'result.json').read_text())['queries']}
summary=[];diffs=[]
for name in ['compact','medium','large','compact_reorder','medium_reorder','large_reorder','medium_merge']:
 profile=json.loads((root/name/'profile.json').read_text());entries=[]
 for p in sorted((root/name).glob('q*/result.json')):
  r=json.loads(p.read_text());q=r['id'];e=expected[q];m=json.loads((p.parent/'metrics.json').read_text())
  entry=dict(id=q,**m,status=r['status'],elapsed_seconds=r.get('elapsed_seconds'))
  if r['status']=='complete':
   av=[json.loads(l) for l in (p.parent/'rows.jsonl').read_text().splitlines()];ev=[json.loads(l) for l in (ref/'queries'/(q+'.rows.jsonl')).read_text().splitlines()]
   entry['comparison']=disposition(dict(r,values=av),dict(e,values=ev))
   if entry['comparison']=='result_mismatch_review_required':
    ds=[dict(row=i,column=j,actual=x,expected=y) for i,(a,b) in enumerate(zip(av,ev)) for j,(x,y) in enumerate(zip(a,b)) if x!=y]
    diffs.append(dict(profile=name,id=q,actual_rows=len(av),expected_rows=len(ev),differences=ds))
  else:entry['error']=r.get('error')
  entries.append(entry)
 summary.append(dict(name=name,profile=profile,counts=dict(collections.Counter(e['status'] for e in entries)),queries=entries))
Path('build/eq197/probe-summary.json').write_text(json.dumps(dict(profiles=summary,differences=diffs),indent=2)+'\n')
for p in summary:print(p['name'],p['counts'],'peakRSS',max(q['peak_rss_bytes'] for q in p['queries']),'peakspill',max(q['peak_spill_bytes'] for q in p['queries']))
for d in diffs:print(d['profile'],d['id'],len(d['differences']))
script=Path('build/eq197/probe-v1.py').read_text();versions=Path('build/eq197/probe-versions');versions.mkdir(exist_ok=True)
v3=script.replace("profiles['compact_reorder']=dict(profiles['compact'],reorder=True)\n",'')
v2=v3.replace("profiles['medium_reorder']=dict(profiles['medium'],reorder=True)\nprofiles['medium_merge']=dict(profiles['medium'],hash_join=False)\n",'').replace("if profile.get('reorder'):os.environ['SAIL_OPTIMIZER__ENABLE_JOIN_REORDER']='true'\nif profile.get('hash_join') is False:os.environ['SAIL_OPTIMIZER__PREFER_HASH_JOIN']='false'\n",'')
for p in summary:
 code={'compact':v2,'medium':v2,'large':v2,'medium_reorder':v3,'compact_reorder':script,'large_reorder':Path('build/eq197/probe.py').read_text(),'medium_merge':Path('build/eq197/probe.py').read_text()}[p['name']]
 assert hashlib.sha256(code.encode()).hexdigest()==p['profile']['fixture_sha256'],p['name']
 (versions/(p['name']+'.py')).write_text(code)
