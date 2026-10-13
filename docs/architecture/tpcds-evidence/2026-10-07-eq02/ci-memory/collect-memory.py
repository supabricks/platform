import gzip,hashlib,inspect,json
from pathlib import Path
import sys
sys.path.insert(0,'install/native')
from analytics import worker_launcher
base=Path('docs/architecture/tpcds-evidence/2026-10-07-eq02');out=base/'ci-memory';out.mkdir(exist_ok=True)
local=Path('build/eq02-merge-review');roots=Path('/data2/supabricks-eq/eq02-merge-review')
def copy(src,name):
 dst=out/name;dst.parent.mkdir(parents=True,exist_ok=True)
 dst.write_bytes(gzip.compress(src.read_bytes(),mtime=0) if name.endswith('.gz') else src.read_bytes())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
proof=json.loads((local/'thp-candidate.json').read_text())
assert (local/'thp-candidate/python/analytics/python').read_text()==worker_launcher('linux-x86_64')
proof['verified_final_launcher_source_sha256']=hashlib.sha256(inspect.getsource(worker_launcher).encode()).hexdigest()
proof['final_packaging_source_sha256']=sha(Path('install/native/analytics.py'))
(local/'thp-candidate-final.json').write_text(json.dumps(proof,indent=2)+'\n')
for name in ['sync-current/sync.json','governed-current/governed.json','current-verification.json','thp-candidate.json','thp-candidate-final.json','package-thp.py','collect-memory.py']:
 copy(local/name,name)
for name in ['assemble-macos-failure','sync-linux-current-failure','baseline-linux-green','baseline-macos-green','packaging-final','harness-final']:
 copy(local/(name+'.log'),name+'.log.gz')
fixtures=[]
for name in ['current-01','thp-always-01','thp-always-02','thp-candidate-01','thp-candidate-02','thp-candidate-03']:
 directory='installed-capacity-'+name;root=roots/directory
 r=json.loads((root/'result.json').read_text());c=json.loads((root/'cleanup.json').read_text())
 assert c['leaked_descendants']==c['remaining_descendants']==0
 for f in ['result.json','cleanup.json']:copy(root/f,directory+'/'+f)
 copy(local/(directory+'.log'),directory+'/output.log.gz')
 h=r['metrics']['history'];fixtures.append(dict(fixture=directory,status=r['status'],checks=len(r['checks']),
  history_seconds=h['elapsed_seconds'],history_highwater_bytes=h['highwater_bytes'],
  append_highwater_bytes=next(x['highwater_bytes'] for x in h['memory'] if x['phase']=='after_append'),
  release_identity=r['release_identity'],cleanup=c))
for name in ['run-capacity-current.sh','run-capacity-thp-always.sh','run-capacity-thp-always-02.sh',*[f'run-capacity-thp-candidate-{i:02}.sh' for i in range(1,4)]]:copy(local/name,name)
summary=dict(status='Local allocator candidate passes; hosted qualification still required',scope='CI repairs only; SP frozen; retained SF1 and Spark reference untouched; #200 deferred until CI and merge',
 previous_head='81cbe523330054d8932e3bc4fd49b406b6743911',issues=[137,201,202,203],fixtures=fixtures,
 validation=dict(packaging_tests=82,harness_tests=13,analytical_baseline='Linux and macOS passed 155 worker tests, 20 fixture tests and synthetic qualification',governed='current-head hosted data suite passes all 17 checks; identity/sync/upgrade/browser also pass'),
 findings=['Hosted history RSS is 851910656 bytes; 838303744 already reached after appends, before readers.',
 'Explicit jemalloc thp:always on the unchanged archive raises local append memory from 258367488 to 736534528 or 791445504 bytes.',
 'Linux launcher enforces thp:never before Python/native allocator initialization. Three candidate runs restore append peaks below 246 MiB, without raising the 768 MiB gate.',
 'The original hosted THP setting was not captured; huge pages are a controlled reproduction of the allocation pattern, not conclusive attribution to that destroyed runner.',
 'Pinned input download connect timeout was cleared by rerunning only failed assembly jobs; new fetch adds bounded transport retries, temporary-file cleanup and checksum-before-publication. Runtime assertion failures are not retried.'])
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(base/'SHA256SUMS').write_text(''.join(sha(p)+'  '+str(p.relative_to(base))+'\n' for p in sorted(base.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print(len(fixtures),'fixtures retained')
