import collections,hashlib,json
from pathlib import Path
local=Path('build/eq206');root=Path('/data2/supabricks-eq/eq206')
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
runs={mode:dict(result=read(root/mode/'product/result.json'),comparison=read(root/mode/'comparison.json'),review=read(root/mode/'review.json'),numeric=read(root/mode/'numerical-review/review.json'),cleanup=read(root/mode/'verify-cleanup.json')) for mode in ['baseline','candidate']}
for mode,data in runs.items():
 r=data['result'];assert r['stopped'] and len(r['tables'])==24 and len(r['queries'])==103
 assert all(t['status']=='PASS' for t in r['tables'])
 assert r['load_receipt_sha256']=='0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f'
 assert r['epoch_id']=='fed0899c-c267-4d8a-8432-3e5e920718af' and r['resource_profile']=='analytical'
 assert data['review']['reviewed_counts']['order_review_pending']==0
 assert not data['review']['execution_failures']
 assert {q['id'] for q in data['review']['value_differences']} <= {'q39a','q39b'}
 assert data['numeric']['status']=='Q39_NUMERICALLY_ACCEPTED'
 assert data['cleanup']['exit_code']==0 and data['cleanup']['leaked_descendants']==data['cleanup']['remaining_descendants']==0
 assert not data['cleanup']['timed_out'] and not data['cleanup']['inspection_failed']
queries={mode:{q['id']:q for q in data['result']['queries']} for mode,data in runs.items()}
for q in queries['baseline']:
 assert queries['baseline'][q]['sql_sha256']==queries['candidate'][q]['sql_sha256']
accepted=lambda d:{q['id'] for q in d['comparison']['queries'] if q['status']=='correct'}|{q['id'] for q in d['review']['order_reviews']}
previous=accepted(runs['baseline']);current=accepted(runs['candidate']);assert not previous-current
counts={mode:dict(strict=data['comparison']['counts'],exact_after_order_review=len(accepted(data)),accepted_with_q39_review=len(accepted(data)|{'q39a','q39b'})) for mode,data in runs.items()}
assert all(c['accepted_with_q39_review']==103 for c in counts.values())
summary=dict(scope='Retained SF1 only; explicit q39 numerical review, SP frozen; no signed-release or larger-scale claim',
 counts=counts, previously_exact_regressions=sorted(previous-current),
 numerical={mode:{q['id']:{engine:q[engine]['oracle_ulps'] for engine in ['product','spark']} for q in data['numeric']['queries']} for mode,data in runs.items()},
 raw_different_cells={mode:{q['id']:q['different_values'] for q in data['review']['value_differences']} for mode,data in runs.items()},
 paired_previously_exact_seconds={mode:sum(queries[mode][q]['elapsed_seconds'] for q in previous) for mode in runs},
 paired_all_query_seconds={mode:sum(q['elapsed_seconds'] for q in qs.values()) for mode,qs in queries.items()},
 full_elapsed_seconds={mode:data['result']['elapsed_seconds'] for mode,data in runs.items()},
 resources={mode:{key:max(q['sampled_resources'][key] for q in qs.values()) for key in ['peak_rss_bytes','peak_spill_bytes','peak_spill_file_bytes']} for mode,qs in queries.items()},
 per_query={q:{mode:dict(seconds=qs[q]['elapsed_seconds'],resources=qs[q]['sampled_resources']) for mode,qs in queries.items()} for q in queries['baseline']},
 interpretation='One same-profile pair; descriptive timings, not a statistically established speedup. Both baseline and candidate can satisfy the oracle; acceptance change is distinct from source arithmetic/semantic fixes.',
 platform_build=read(Path('build/eq197/platform-artifact/platform-build.json')),sail_build=read(local/'sail-artifact/sail-build.json'),package=read(local/'package.json'),
 cleanup={mode:data['cleanup'] for mode,data in runs.items()},
 report_hashes={mode:{name:sha(root/mode/name) for name in ['comparison.json','review.json','numerical-review/review.json','product/result.json','verify-cleanup.json']} for mode in runs})
(local/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
print(json.dumps({k:summary[k] for k in ['counts','numerical','raw_different_cells','paired_previously_exact_seconds','paired_all_query_seconds','full_elapsed_seconds','resources']},indent=2))
