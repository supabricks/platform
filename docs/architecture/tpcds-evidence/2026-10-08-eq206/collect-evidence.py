import gzip,hashlib
from pathlib import Path
local=Path('build/eq206');runs=Path('/data2/supabricks-eq/eq206')
out=Path('docs/architecture/tpcds-evidence/2026-10-08-eq206');out.mkdir(exist_ok=True)
def copy(src,name):
 dst=out/name;dst.parent.mkdir(parents=True,exist_ok=True)
 dst.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if dst.suffix=='.gz' else src.read_bytes())
for name in ['package.py','package.json','copy.json','run-upgrade.sh','upgrade-result.json','run-qualification.sh','run-sql-tests.sh','review-results.py','collect-evidence.py','summarize.py','summary.json','baseline-source.lock.json','final-reviews.sh','replay-evidence.py','host-during-baseline.json','host-during-candidate.json']:
 copy(local/name,name)
for name in ['rust-all-tests','codec-tests','clippy-final','harness-tests','component-validation','archive-replay','final-reviews','sail-build','package','upgrade','baseline-verify','baseline-compare','candidate-verify','candidate-compare']:
 copy(local/(name+'.log'),name+'.log.gz')
for name in ['sail-build.json','Cargo.lock']:
 copy(local/'sail-artifact'/name,'sail-artifact/'+name)
copy(Path('components/sail-source.lock.json'),'sail-artifact/source.lock.json')
copy(Path('build/eq197/platform-artifact/platform-build.json'),'platform-artifact/platform-build.json')
for mode in ['baseline','candidate']:
 for name in ['comparison.json','review.json','verify-cleanup.json','product/result.json','numerical-review/review.json','numerical-review/strict-recomputed.json']:
  copy(runs/mode/name,mode+'/'+name)
 for p in sorted((runs/mode/'product').glob('*/result.json')):
  copy(p,mode+'/product/'+str(p.relative_to(runs/mode/'product')))
 for p in sorted((runs/mode/'product/queries').iterdir()):
  if p.is_file():copy(p,mode+'/product/queries/'+p.name+'.gz')
for mode in ['baseline','spark','candidate','cluster','candidate-first','candidate-second','cluster-second','spark-initial']:
 copy(runs/('sql-'+mode)/'pytest.xml','sql/'+mode+'/pytest.xml.gz')
for mode in ['baseline','spark','candidate','cluster']:
 copy(local/('sql-'+mode+'.log'),'sql/'+mode+'/pytest.log.gz')
for p in (local/'python-tests').glob('*.py'):copy(p,'sql/fixtures/'+p.name)
for stage in ['first-candidate','second-candidate']:
 for name in ['package.json','source.lock.json','sql-candidate.log']:
  copy(local/stage/name,stage+'/'+name+('.gz' if name.endswith('.log') else ''))
 copy(local/stage/'sail-artifact/sail-build.json',stage+'/sail-build.json')
for name in ['verify.py','session_metrics.py','sail_candidate.py','platform_candidate.py','compare.py','package_sail_candidate.py','q39_review.py','q39-review.lock.json','test_q39_review.py']:
 copy(Path('e2e/tpcds')/name,'harness/'+name)
for name in ['reduced.py','reduced-v1.py','run-reduced.sh','extract-all-groups.py','run-extract.sh','oracle.py','model.py','order-envelope.py','probe.py','run-probe.sh']:
 copy(local/name,'diagnostics/'+name)
for name in ['all-groups','oracle','order-envelope','model','groups']:
 copy(runs/'diagnostics'/(name+'.json'),'diagnostics/'+name+'.json.gz')
for mode in ['baseline','baseline1','spark','spark1','candidate','candidate1']:
 copy(runs/('reduced-'+mode)/'result.json','diagnostics/reduced-'+mode+'.json')
 copy(local/('reduced-'+mode+'.log'),'diagnostics/reduced-'+mode+'.log.gz')
for profile in ['large','large_reorder']:
 for p in sorted((runs/'probes'/profile).rglob('*')):
  if p.is_file():copy(p,'probes/'+str(p.relative_to(runs/'probes'))+'.gz')
# Deliberately exclude private state, tokens, backup catalogs and database files.
(out/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(out))+'\n' for p in sorted(out.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('evidence files',sum(p.is_file() for p in out.rglob('*')))
