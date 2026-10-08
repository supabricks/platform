import collections,hashlib,json
from pathlib import Path
root=Path('/data2/supabricks-eq/eq200');local=Path('build/eq200')
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
ids=['q7','q9','q18','q26','q27','q28','q57','q63','q89']
product={mode:read(root/mode/'product/result.json') for mode in ['baseline','candidate']}
ledger={mode:read(root/mode/'comparison.json') for mode in product}
review={mode:read(root/mode/'review.json') for mode in product}
queries={mode:{q['id']:q for q in report['queries']} for mode,report in product.items()}
status={mode:{q['id']:q['status'] for q in report['queries']} for mode,report in ledger.items()}
for mode in status:
    for entry in review[mode]['order_reviews']:status[mode][entry['id']]='correct'
changes=[dict(id=q,before=status['baseline'][q],after=status['candidate'][q]) for q in status['baseline'] if status['baseline'][q]!=status['candidate'][q]]
regressions=[x for x in changes if x['after']!='correct']
measurements=[dict(id=q,before=status['baseline'][q],after=status['candidate'][q],
                   baseline_seconds=queries['baseline'][q]['elapsed_seconds'],
                   candidate_seconds=queries['candidate'][q]['elapsed_seconds'],
                   baseline_session_start_seconds=queries['baseline'][q]['session_start_seconds'],
                   candidate_session_start_seconds=queries['candidate'][q]['session_start_seconds']) for q in ids]
cleanup={mode:read(root/mode/'verify-cleanup.json') for mode in product}
for report in cleanup.values():
    assert report['exit_code']==0 and not report['timed_out'] and not report['inspection_failed']
    assert report['leaked_descendants']==report['remaining_descendants']==0
assert all(q['before']=='result_mismatch_review_required' and q['after']=='correct' for q in measurements)
assert not regressions
assert product['baseline']['epoch_id']==product['candidate']['epoch_id']
assert product['baseline']['fixture_sha256']==product['candidate']['fixture_sha256']
assert product['baseline']['load_receipt_sha256']==product['candidate']['load_receipt_sha256']=='0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f'
assert all(len(r['tables'])==24 and len(r['queries'])==103 and r['stopped'] for r in product.values())
result=dict(status='PASS_ISSUE_200',overall_sf1_status='INCOMPLETE',scope='EQ02 retained SF1 engineering correction; SP frozen',
            sail_commit='968537980e77c52d397df9553db536a8fda50fc6',
            package_proof_sha256=sha(local/'package.json'),
            profile=dict(cpus=list(range(8)),memory_bytes=16*1024**3,swap=False,network='none',query_timeout_seconds=120),
            load_receipt_sha256=product['baseline']['load_receipt_sha256'],epoch_id=product['baseline']['epoch_id'],
            reference_sha256=sha(Path('/data2/supabricks-eq/eq02-20261007/reference-02/result.json')),
            releases={mode:r['release_identity'] for mode,r in product.items()},
            raw_counts={mode:r['counts'] for mode,r in ledger.items()},
            reviewed_counts={mode:r['reviewed_counts'] for mode,r in review.items()},
            changed_dispositions=changes,new_regressions=regressions,affected_queries=measurements,
            affected_query_seconds={mode:sum(q[mode+'_seconds'] for q in measurements) for mode in product},
            timing_scope='One paired complete-suite run under identical declared limits; descriptive timings, not a statistical speedup or throughput claim; fixed run order and host/page-cache effects are not isolated.',
            reduced_cases=17,pytest_cases_per_engine=18,pytest_engines=['Spark 4.2.0 JVM','Sail local','Sail local-cluster'],rust_regressions=5,
            cleanup=cleanup,remaining_issues=[197,198,199])
(local/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['status','reviewed_counts','affected_query_seconds','new_regressions']},indent=2))
