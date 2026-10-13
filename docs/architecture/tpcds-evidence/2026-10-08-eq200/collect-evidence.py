import gzip,hashlib,json
from pathlib import Path
local=Path('build/eq200');runs=Path('/data2/supabricks-eq/eq200')
out=Path('docs/architecture/tpcds-evidence/2026-10-08-eq200');out.mkdir(exist_ok=True)
def copy(src,name):
    target=out/name;target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if target.suffix=='.gz' else src.read_bytes())
for name in ['package.py','package.json','run-upgrade.sh','upgrade-result.json','run-qualification.sh','run-sql-tests.sh','run-reduced.sh','reduced.py','collect-evidence.py','summarize.py','review-results.py','summary.json']:
    copy(local/name,name)
for name in ['harness-tests','artifact-contract-tests','rust-tests','codec-tests','clippy','format','check','python-lint','sail-build','package','upgrade','sql-spark','sql-candidate','sql-cluster','baseline-verify','candidate-verify','baseline-compare','candidate-compare','baseline-review','baseline-review-02','candidate-review','reduced-candidate']:
    copy(local/(name+'.log'),name+'.log.gz')
for name in ['sail-build.json','Cargo.lock']:
    copy(local/'sail-artifact'/name,'sail-artifact/'+name)
copy(Path('components/sail-source.lock.json'),'sail-artifact/source.lock.json')
for mode in ['spark','candidate','cluster']:
    copy(runs/('sql-'+mode)/'pytest.xml','sql-'+mode+'/pytest.xml')
for name in ['conftest.py','test_decimal_avg.py']:
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
for name in ['sail.json','spark.json','comparison.json']:
    copy(Path('/data2/supabricks-eq/eq200-preflight')/name,'reduced/before/'+name)
copy(runs/'reduced-candidate.json','reduced/after/candidate.json')
copy(runs/'reduced-comparison.json','reduced/after/comparison.json')
(out/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(out))+'\n' for p in sorted(out.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('evidence files',sum(p.is_file() for p in out.rglob('*')))
