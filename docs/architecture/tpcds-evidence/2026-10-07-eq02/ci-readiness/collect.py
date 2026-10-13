import gzip,hashlib,json,shutil
from pathlib import Path
base=Path('docs/architecture/tpcds-evidence/2026-10-07-eq02');out=base/'ci-readiness';out.mkdir(exist_ok=True)
local=Path('build/eq02-merge-review');roots=Path('/data2/supabricks-eq/eq02-merge-review')
def copy(src,name):
 dst=out/name;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if dst.suffix=='.gz' else src.read_bytes())
for name in ['ci-installation.json','package-candidate.json','repeats.json','collect.py']:
 if (local/name).exists():copy(local/name,name)
for name in ['harness-tests','governed-tests','local-tests','native-build']:
 copy(local/(name+'.log'),name+'.log.gz')
for name in ['sync/sync.json','governed/governed.json']:copy(local/name,'failed-ci/'+name)
for name in ['baseline','sync','governed']:copy(Path('/tmp')/('eq02-'+name+'.log'),'failed-ci/'+name+'.log.gz')
results=[]
for root in sorted(roots.iterdir()):
 if not root.is_dir() or not (root/'result.json').exists():continue
 r=json.loads((root/'result.json').read_text());c=json.loads((root/'cleanup.json').read_text())
 assert c['leaked_descendants']==c['remaining_descendants']==0
 for name in ['result.json','cleanup.json']:copy(root/name,root.name+'/'+name)
 log=local/(root.name+'.log')
 if log.exists():copy(log,root.name+'/output.log.gz')
 results.append(dict(fixture=root.name,status=r['status'],checks=len(r['checks']),cleanup=c,
  history_highwater_bytes=r.get('metrics',{}).get('history',{}).get('highwater_bytes')))
(out/'summary.json').write_text(json.dumps(dict(status='Local candidates qualified; current-head hosted CI required before merge',issues=[137,201,202],scope='No SP trial or TPC-DS reload; #200 follows CI and merge',source_commits={'delta_workflow':'9dee851','capacity_evidence':'f2c9fe1','governed_readiness':'e37664f'},unit_tests=224,ignored_tests=4,daemon_tests=1,harness_tests=13,fixtures=results,limitations=['Original capacity high-water value was lost; failure retained, local exact-archive repeats do not establish its cause.','Governed admission gap is corrected; original generic post-prepare CI denial was not reproduced locally.','Local native overlay is not an exact release archive; original CI archive is verified separately.']),indent=2)+'\n')
(base/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(base))+'\n' for p in sorted(base.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('fixtures',len(results))
