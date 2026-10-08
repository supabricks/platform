import gzip,hashlib,json
from pathlib import Path
local=Path('build/eq198');runs=Path('/data2/supabricks-eq/eq198')
out=Path('docs/architecture/tpcds-evidence/2026-10-08-eq198');out.mkdir(exist_ok=True)
def copy(src,name):
 dst=out/name;dst.parent.mkdir(parents=True,exist_ok=True)
 dst.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if dst.suffix=='.gz' else src.read_bytes())
for name in ['package.py','package.json','copy.json','run-upgrade.sh','upgrade-result.json','run-qualification.sh','run-sql-tests.sh','run-regression-tests.sh','review-results.py','collect-evidence.py','summarize.py','summary.json','baseline-source.lock.json']:
 copy(local/name,name)
for name in ['rust-tests','clippy','python-lint','sail-build','package','upgrade','baseline-verify','baseline-compare','candidate-verify','candidate-compare']:
 copy(local/(name+'.log'),name+'.log.gz')
for name in ['sail-build.json','Cargo.lock']:
 copy(local/'sail-artifact'/name,'sail-artifact/'+name)
copy(Path('components/sail-source.lock.json'),'sail-artifact/source.lock.json')
copy(Path('build/eq197/platform-artifact/platform-build.json'),'platform-artifact/platform-build.json')
for mode in ['baseline','candidate']:
 for name in ['comparison.json','review.json','verify-cleanup.json','product/result.json']:
  copy(runs/mode/name,mode+'/'+name)
 for p in sorted((runs/mode/'product').glob('*/result.json')):
  copy(p,mode+'/product/'+str(p.relative_to(runs/mode/'product')))
 for p in sorted((runs/mode/'product/queries').iterdir()):
  if p.is_file():copy(p,mode+'/product/queries/'+p.name+'.gz')
for mode in ['baseline','spark','candidate','cluster','candidate-first','cluster-first','candidate-second','baseline-dataframe','spark-dataframe-initial']:
 p=runs/('sql-'+mode)/'pytest.xml'
 copy(p,'reduced/'+mode+'/pytest.xml.gz')
# The first two candidates fail only their cast cases. Their artifacts remain
# local; retain the receipts, expected source pins, and diagnostics publicly.
for stage in ['first-candidate','second-candidate']:
 for name in ['package.json','sail-source.lock.json','sql-candidate.log']:
  copy(local/stage/name,stage+'/'+name+('.gz' if name.endswith('.log') else ''))
 copy(local/stage/'sail-artifact/sail-build.json',stage+'/sail-build.json')
for mode in ['baseline','spark','candidate','cluster']:
 copy(local/('sql-'+mode+'.log'),'reduced/'+mode+'/pytest.log.gz')
for mode in ['baseline-dataframe','spark-dataframe-initial']:
 copy(local/('sql-'+mode+'.log'),'reduced/'+mode+'/pytest.log.gz')
for mode in ['candidate','cluster']:
 copy(runs/('regression-'+mode)/'pytest.xml','regression/'+mode+'/pytest.xml.gz')
 copy(local/('regression-'+mode+'.log'),'regression/'+mode+'/pytest.log.gz')
for folder in ['python-tests','regression-tests']:
 for p in (local/folder).glob('*.py'):copy(p,'reduced/'+folder+'/'+p.name)
for name in ['verify.py','session_metrics.py','sail_candidate.py','platform_candidate.py','compare.py','package_sail_candidate.py']:
 copy(Path('e2e/tpcds')/name,'harness/'+name)
for name in ['reduced.py','run-reduced.sh','debug.py','run-debug.sh','debug-cast.py','run-debug-cast.sh']:
 copy(local/name,'diagnostics/'+name)
for name in ['debug.log','debug-cast.log']:
 copy(local/name,'diagnostics/'+name+'.gz')
copy(runs/'reduced-baseline/results.json','diagnostics/reduced-baseline.json')
(out/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(out))+'\n' for p in sorted(out.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('evidence files',sum(p.is_file() for p in out.rglob('*')))
