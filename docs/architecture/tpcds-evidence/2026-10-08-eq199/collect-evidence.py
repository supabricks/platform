import gzip,hashlib,json
from pathlib import Path
local=Path('build/eq199');runs=Path('/data2/supabricks-eq/eq199')
out=Path('docs/architecture/tpcds-evidence/2026-10-08-eq199');out.mkdir(exist_ok=True)
def copy(src,name):
    target=out/name;target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if target.suffix=='.gz' else src.read_bytes())
for name in ['package.py','package.json','run-upgrade.sh','upgrade-result.json','run-baseline.sh','run-qualification.sh','run-sql-tests.sh','run-reduced.sh','run-candidate.sh','reduced.py','compare-reduced.py','collect-evidence.py','summarize.py','review-results.py','summary.json','baseline-source.lock.json']:
    copy(local/name,name)
for name in ['harness-tests','artifact-contract-tests','sail-contract-tests','rust-decimal-null','rust-codec','clippy-final','format','cargo-check','python-lint','sail-build','package','upgrade','sql-spark','sql-candidate','sql-cluster','baseline','candidate-verify','candidate-compare','candidate-review','reduced-baseline','reduced-spark','reduced-candidate','reduced-comparison']:
    copy(local/(name+'.log'),name+'.log.gz')
for name in ['sail-build.json','Cargo.lock']:
    copy(local/'sail-artifact'/name,'sail-artifact/'+name)
copy(Path('components/sail-source.lock.json'),'sail-artifact/source.lock.json')
for mode in ['spark','candidate','cluster']:
    copy(runs/('sql-'+mode)/'pytest.xml','sql-'+mode+'/pytest.xml')
for name in ['conftest.py','test_decimal_avg.py','test_decimal_arithmetic.py']:
    copy(local/'python-tests'/name,'python-tests/'+name)
for mode in ['baseline','candidate']:
    for name in ['comparison.json','review.json','verify-cleanup.json','product/result.json']:
        copy(runs/mode/name,mode+'/'+name)
    for p in sorted((runs/mode/'product').glob('*/result.json')):
        copy(p,mode+'/product/'+str(p.relative_to(runs/mode/'product')))
    for p in sorted((runs/mode/'product/queries').iterdir()):
        if p.is_file():copy(p,mode+'/product/queries/'+p.name+'.gz')
for name in ['verify.py','sail_candidate.py','package_sail_candidate.py','compare.py','test_sail_candidate.py']:
    copy(Path('e2e/tpcds')/name,'harness/'+name)
for mode in ['baseline','spark','candidate']:
    copy(runs/('reduced-'+mode)/'results.json','reduced/'+mode+'.json')
copy(runs/'reduced-comparison.json','reduced/comparison.json')
# Retain the rejected first candidate, including all statements and the reduced
# correlated-aggregate reproduction; it is not accepted qualification evidence.
for name in ['comparison.json','verify-cleanup.json','product/result.json']:
    copy(runs/'candidate-first'/name,'first-candidate/'+name)
for p in sorted((runs/'candidate-first/product/queries').iterdir()):
    if p.is_file():copy(p,'first-candidate/product/queries/'+p.name+'.gz')
for name in ['package.json','source.lock.json','upgrade-result.json']:
    copy(local/'first-candidate'/name,'first-candidate/'+name)
for name in ['sail-build','candidate-verify','candidate-compare','candidate-review']:
    copy(local/'first-candidate'/(name+'.log'),'first-candidate/'+name+'.log.gz')
copy(local/'first-candidate/sail-artifact/sail-build.json','first-candidate/sail-build.json')
copy(local/'sql-candidate-null-repro.log','first-candidate/null-reproduction.log.gz')
copy(runs/'sql-candidate-null-repro/pytest.xml','first-candidate/null-reproduction.xml')
(out/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(out))+'\n' for p in sorted(out.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('evidence files',sum(p.is_file() for p in out.rglob('*')))
