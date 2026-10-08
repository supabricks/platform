import gzip,hashlib,json
from pathlib import Path
local=Path('build/eq197');runs=Path('/data2/supabricks-eq/eq197')
out=Path('docs/architecture/tpcds-evidence/2026-10-08-eq197');out.mkdir(exist_ok=True)
def copy(src,name):
 dst=out/name;dst.parent.mkdir(parents=True,exist_ok=True)
 dst.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if dst.suffix=='.gz' else src.read_bytes())
for name in ['package.py','package.json','run-upgrade.sh','upgrade-result.json','run-qualification.sh','run-probe.sh','probe.py','probe-summary.json','summarize-probes.py','review-results.py','collect-evidence.py','summarize.py','summary.json']:
 copy(local/name,name)
for name in ['analytics-tests','profile-test','lib-tests','python-tests','harness-tests','package','upgrade','baseline-verify','baseline-compare','candidate-verify','candidate-compare']:
 copy(local/(name+'.log'),name+'.log.gz')
copy(local/'platform-artifact/platform-build.json','platform-artifact/platform-build.json')
copy(local/'platform-artifact/build.log','platform-artifact/build.log.gz')
copy(local/'platform-artifact/python/analytics/session.py','platform-artifact/session.py')
for name in ['sail-build.json','Cargo.lock']:
 copy(Path('build/eq199/sail-artifact')/name,'sail-artifact/'+name)
copy(Path('components/sail-source.lock.json'),'sail-artifact/source.lock.json')
for mode in ['baseline','candidate']:
 for name in ['comparison.json','review.json','verify-cleanup.json','product/result.json']:
  copy(runs/mode/name,mode+'/'+name)
 for p in sorted((runs/mode/'product').glob('*/result.json')):
  copy(p,mode+'/product/'+str(p.relative_to(runs/mode/'product')))
 for p in sorted((runs/mode/'product/queries').iterdir()):
  if p.is_file():copy(p,mode+'/product/queries/'+p.name+'.gz')
for mode in ['compact','medium','large','compact_reorder','medium_reorder','large_reorder','medium_merge']:
 copy(local/'probe-versions'/(mode+'.py'),'probes/'+mode+'/probe.py')
 copy(runs/'probes'/mode/'profile.json','probes/'+mode+'/profile.json')
 for p in sorted((runs/'probes'/mode).glob('q*/*')):
  if p.is_file():
   name='probes/'+mode+'/'+str(p.relative_to(runs/'probes'/mode))
   copy(p,name+('.gz' if p.suffix in ('.jsonl','.log','.txt') else ''))
for name in ['verify.py','session_metrics.py','sail_candidate.py','platform_candidate.py','build_platform_candidate.py','package_platform_candidate.py','compare.py','test_platform_candidate.py']:
 copy(Path('e2e/tpcds')/name,'harness/'+name)
for name in ['comparison.json','verify-cleanup.json','product/result.json']:
 copy(runs/'baseline-rejected'/name,'baseline-rejected/'+name)
copy(local/'baseline-rejected-verify.log','baseline-rejected/verify.log.gz')
for name in ['comparison.json','review.json','verify-cleanup.json','product/result.json']:
 copy(runs/'candidate-first'/name,'first-candidate/'+name)
for p in sorted((runs/'candidate-first/product/queries').iterdir()):
 if p.is_file():copy(p,'first-candidate/product/queries/'+p.name+'.gz')
for name in ['package.json','upgrade-result.json','verify-candidate.py']:
 copy(local/'first-candidate'/name,'first-candidate/'+name)
for name in ['candidate-verify','candidate-compare']:
 copy(local/'first-candidate'/(name+'.log'),'first-candidate/'+name+'.log.gz')
copy(local/'first-candidate/platform-artifact/platform-build.json','first-candidate/platform-build.json')
copy(local/'first-candidate/platform-artifact/python/analytics/session.py','first-candidate/session.py')
(out/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(out))+'\n' for p in sorted(out.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('evidence files',sum(p.is_file() for p in out.rglob('*')))
