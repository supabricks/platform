import collections,hashlib,json
from pathlib import Path
local=Path('build/eq198');root=Path('/data2/supabricks-eq/eq198')
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
runs={mode:dict(result=read(root/mode/'product/result.json'),comparison=read(root/mode/'comparison.json'),review=read(root/mode/'review.json'),cleanup=read(root/mode/'verify-cleanup.json')) for mode in ['baseline','candidate']}
for mode,data in runs.items():
 assert data['result']['stopped'] and len(data['result']['tables'])==24 and len(data['result']['queries'])==103
 assert all(t['status']=='PASS' for t in data['result']['tables'])
 assert data['result']['load_receipt_sha256']=='0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f'
 assert data['result']['epoch_id']=='fed0899c-c267-4d8a-8432-3e5e920718af'
 assert data['result']['resource_profile']=='analytical'
 assert data['review']['reviewed_counts']['order_review_pending']==0
 assert data['cleanup']['exit_code']==0
 assert data['cleanup']['leaked_descendants']==data['cleanup']['remaining_descendants']==0
 assert not data['cleanup']['timed_out'] and not data['cleanup']['inspection_failed']
b,c=runs['baseline'],runs['candidate']
accepted=lambda data:{q['id'] for q in data['comparison']['queries'] if q['status']=='correct'}|{q['id'] for q in data['review']['order_reviews']}
previous=accepted(b);now=accepted(c);regressions=sorted(previous-now)
queries={mode:{q['id']:q for q in d['result']['queries']} for mode,d in runs.items()}
for q in queries['baseline']:
 assert queries['baseline'][q]['sql_sha256']==queries['candidate'][q]['sql_sha256'],q
affected='q6 q30 q41 q81'.split()
paired=[dict(id=q,baseline_seconds=queries['baseline'][q]['elapsed_seconds'],candidate_seconds=queries['candidate'][q]['elapsed_seconds'],baseline_peak_rss=queries['baseline'][q]['sampled_resources']['peak_rss_bytes'],candidate_peak_rss=queries['candidate'][q]['sampled_resources']['peak_rss_bytes']) for q in sorted(previous)]
summary=dict(scope='Retained SF1 engineering qualification; SP frozen; no larger-scale or signed-release claim',
 platform_build=read(Path('build/eq197/platform-artifact/platform-build.json')),sail_build=read(local/'sail-artifact/sail-build.json'),package=read(local/'package.json'),
 original_alias_failures=affected,affected_results={q:dict(status=queries['candidate'][q]['status'],seconds=queries['candidate'][q]['elapsed_seconds'],result_sha256=queries['candidate'][q].get('result_sha256')) for q in affected},all_original_alias_failures_execute=all(queries['candidate'][q]['status']=='complete_requires_reference_comparison' for q in affected),
 baseline_counts=b['review']['reviewed_counts'],candidate_counts=c['review']['reviewed_counts'],previously_correct_regressions=regressions,
 newly_correct=sorted(now-previous),remaining_failures=c['review']['execution_failures'],remaining_values=c['review']['value_differences'],
 paired_previously_correct=paired,paired_previously_correct_seconds={mode:sum(queries[mode][q]['elapsed_seconds'] for q in previous) for mode in runs},
 full_elapsed_seconds={mode:d['result']['elapsed_seconds'] for mode,d in runs.items()},
 resources={mode:{key:max(q['sampled_resources'][key] for q in d['result']['queries'] if 'sampled_resources' in q) for key in ['peak_rss_bytes','peak_spill_bytes','peak_spill_file_bytes']} for mode,d in runs.items()},
 cleanup={mode:d['cleanup'] for mode,d in runs.items()},
 comparison='Exact positional SQL types and values; only individually reviewed ORDER BY/unordered cases; no numeric tolerance',
 interpretation='Single paired run, descriptive elapsed times; failed baseline statements are not speedup denominators.',
 rejected_reduced_candidates=['b9d0fd85c5134f857e101f138c7ca01d9d650e93', '3e538c6b312a75a33e52bf6c36f7dcd9da475b98'],
 report_hashes={mode:{name:sha(root/mode/name) for name in ['comparison.json','review.json','product/result.json','verify-cleanup.json']} for mode in runs})
(local/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
assert not regressions,regressions
assert summary['all_original_alias_failures_execute']
print(json.dumps({k:summary[k] for k in ['baseline_counts','candidate_counts','previously_correct_regressions','paired_previously_correct_seconds','resources']},indent=2))
