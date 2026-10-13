import gzip,hashlib,json,shutil
from pathlib import Path
base=Path('docs/architecture/tpcds-evidence/2026-10-07-eq02');out=base/'snapshot-recovery';out.mkdir(exist_ok=True)
local=Path('build/eq03-recovery');root=Path('/data2/supabricks-eq/eq03-recovery/qualification-01')
def copy(src,name):
 dst=out/name;dst.parent.mkdir(parents=True,exist_ok=True)
 dst.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if dst.suffix=='.gz' else src.read_bytes())
for name in ['package.py','package-native.json','package-upgrade.json','run-upgrade.sh','upgrade-result.json','run-verify.sh','attach-result.json','provenance.json','review-results.py','collect-evidence.py']:
 copy(local/name,name)
for name in ['analytics-tests','recovery-tests','harness-tests','native-build','verify','compare','upgrade','attach','review']:
 copy(local/(name+'.log'),name+'.log.gz')
for name in ['comparison.json','review.json','verify-cleanup.json','product/result.json']:
 copy(root/name,name)
for p in sorted((root/'product').glob('*/result.json')):copy(p,'product/'+str(p.relative_to(root/'product')))
for p in sorted((root/'product/queries').iterdir()):
 if p.is_file():copy(p,'product/queries/'+p.name+'.gz')
for name in ['verify.py','compare.py']:copy(Path('e2e/tpcds')/name,name)
# Minimal restart diagnostic excerpts already sanitized; retain failure details.
for name in ['supervisor.log.verify-failure.txt','process-compose.log.verify-failure.txt']:
 copy(Path('build/eq03-capacity')/name,'failed-restart/'+name+'.gz')
summary=dict(status='INCOMPLETE',scope='SF1 engineering end-to-end qualification; SP remains frozen',native_fix='a721740740282f48253b5839d21019fa003e3230',issues=[196,197,198,199,200],exact_tables=24,exact_rows=19557335,statements_attempted=103,statements_executed=90,raw_counts=json.loads((root/'comparison.json').read_text())['counts'],reviewed_counts=json.loads((root/'review.json').read_text())['reviewed_counts'],cleanup=json.loads((root/'verify-cleanup.json').read_text()),limitations=['Unsigned engineering runtime, not exact signed archive qualification.','Supported native-only upgrade and explicit copied-worktree attach are recorded; original load receipt unchanged.','No elapsed restart performance claim: health wait includes explicit relocation attach.','Decimal mismatches remain failures; no tolerance or coercion.','Order reviews apply only to four named SQL statements with exact typed multisets and verified ordering keys.','CI at prior commit passes 33 jobs but macOS notebook recovery fails (#136); candidate CI remains separate.'])
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(base/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(base))+'\n' for p in sorted(base.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('recovery evidence files',sum(p.is_file() for p in out.rglob('*')))
