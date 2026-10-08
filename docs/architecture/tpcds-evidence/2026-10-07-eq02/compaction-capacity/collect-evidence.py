import gzip,hashlib,json,shutil
from pathlib import Path
base=Path('docs/architecture/tpcds-evidence/2026-10-07-eq02');out=base/'compaction-capacity';out.mkdir(exist_ok=True)
def copy(src,dst):
 src=Path(src);dst=out/dst;dst.parent.mkdir(parents=True,exist_ok=True)
 if dst.suffix=='.gz':dst.write_bytes(gzip.compress(src.read_bytes(),mtime=0))
 else:shutil.copyfile(src,dst)
local=Path('build/eq03-capacity')
for name in ['package-native-01.json','package-01.json','package-native-02.json','package-02.json','replay-load10.py','profile-wide.py','profile-wide-scan.py','run-replays.sh','run-capacity.sh','run-capacity-02.sh','run-checks.sh','collect-evidence.py']:
 copy(local/name,name)
for name in ['worker-tests','maintenance-tests','rust-compaction-tests','rust-sync-tests','rust-analytics-tests','native-build','worker-tests-rollover','rust-compaction-rollover','native-build-rollover','worker-tests-bounded-write','packaging-tests','harness-tests','adapter-contract-tests-02','delta-build-01','replay-predecessor-01','replay-candidate-01','wide-profile-01','wide-profile-02','wide-profile-03','wide-profile-channel1','wide-profile-onecpu','wide-profile-scan']:
 copy(local/(name+'.log'),name+'.log.gz')
copy(local/'delta-artifact-01/deltalake-build.json','delta-build.json')
for name in ['load10-freshness.json','analyze-load.py','commit-freshness.py','supplement-memory.py']:
 copy(Path('build/eq03-append')/name,name)
for name in ['summary.json','publication-timings.json.gz']:
 copy(Path('build/eq03-append/load10-analysis')/name,'load10-analysis/'+name) if not name.endswith('.gz') else shutil.copyfile(Path('build/eq03-append/load10-analysis')/name,out/'load10-analysis'/name)
load=Path('/data2/supabricks-eq/eq03-append/load-10')
for name in ['apply-memory.json','supplement-memory.json','cleanup.json','load/result.json']:
 copy(load/name,'load-10/'+name)
for name in ['apply-memory.samples.jsonl','supplement-memory.samples.jsonl','load/commits.jsonl','load/observations.jsonl']:
 copy(load/name,'load-10/'+name+'.gz')
copy('build/eq03-append/load-10.log','load-10/output.log.gz')
roots=Path('/data2/supabricks-eq/eq03-capacity')
for root in sorted(roots.glob('installed-*-0[12]')):
 if not (root/'result.json').exists() or not (root/'cleanup.json').exists():continue
 for name in ['result.json','cleanup.json']:copy(root/name,root.name+'/'+name)
 copy(local/(root.name+'.log'),root.name+'/output.log.gz')
# Early failures and scanner-only controls retain inherited source measurements;
# their own phase evidence is in logs, not those copied JSON fields.
for root in [roots/n for n in ['wide-profile-03','wide-profile-channel1','wide-profile-onecpu']]:
 if (root/'measurements.json').exists():copy(root/'measurements.json',root.name+'/measurements.json')
copy(roots/'installed-capacity-01/tmp/sy08-oc0vt670/measurements.json','installed-capacity-01/measurements.json')
for root in ['replay-predecessor-01','replay-candidate-01','replay-candidate-02']:
 if (roots/root/'cleanup.json').exists():copy(roots/root/'cleanup.json',root+'/cleanup.json')
 if (local/(root+'.log')).exists():copy(local/(root+'.log'),root+'/output.log.gz')
load11=roots/'load-11'
for name in ['apply-memory.json','cleanup.json','load/result.json','product/result.json','verify-cleanup.json']:
 copy(load11/name,'load-11/'+name)
for name in ['apply-memory.samples.jsonl','load/commits.jsonl','load/observations.jsonl']:
 copy(load11/name,'load-11/'+name+'.gz')
copy(local/'load-11.log','load-11/output.log.gz')
copy(local/'verify-11.log','load-11/verify-output.log.gz')
for name in ['summary.json','publication-timings.json.gz']:
 source=Path('build/eq03-recovery/load11-analysis')/name
 if name.endswith('.gz'):shutil.copyfile(source,out/'load11-publication-timings.json.gz')
 else:copy(source,'load11-analysis.json')
copy(roots/'replay-candidate-02/verification.json','replay-candidate-02/verification.json')
reports=[]
for p in sorted(out.glob('installed-*-02/result.json')):
 r=json.loads(p.read_text());reports.append(dict(suite=r['suite'],status=r['status'],checks=len(r['checks']),release_identity=r['release_identity']))
(out/'summary.json').write_text(json.dumps(dict(status='Full SF1 load and exact tables PASS; 103 SQL attempted, 69 correct after review, 34 tracked failures/mismatches',
 issues=[191,192,193,194,195],source_commits=dict(capacity='dd455da',append_rollover='a425278',sampler_adapter='6940e6d',bounded_write='c9f8723'),
 full_sf1_pass=True,full_sf1_rows=19557335,full_sf1_seconds=4431.261933261994,product_statements_attempted=103,product_statements_completed=90,product_statements_correct_after_review=69,product_statements_required=103,query_evidence='../snapshot-recovery/review.json',
 worker_tests=155,packaging_tests=75,harness_tests=11,installed_suites=reports,
 retained_capacity_replay=dict(predecessor='incremental_disk_budget',candidate_seconds=94.40516478,candidate_highwater_bytes=405692416,scope='diagnostic; production bounds unchanged'),
 limits=dict(worker_rss_bytes=768*1024**2,generation_bytes=1024**3,retained_root_bytes=4*1024**3,delta_versions=1024),
 limitations=['Attempt10 failed; original observer has fork/exec coverage gaps (#191).','Supplemental observer began mid-run and consumed181.27CPU seconds; peaks are observed lower bounds.','Replay/fixture process-owned high-water measurements are unaffected.','Native wheel and runtime are verified engineering overlays, not signed archive qualification.','No SP restart or general merge-buffer fix (#190).']),indent=2)+'\n')
(base/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(base))+'\n' for p in sorted(base.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
print('collected',len(reports),'installed suites')
