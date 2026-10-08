import collections,hashlib,json
from pathlib import Path
root=Path('/data2/supabricks-eq/eq199');local=Path('build/eq199')
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
ids=['q2','q12','q20','q31','q36','q49','q58','q59','q61','q83','q90','q98']
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
assert all(q['before']=='type_mismatch' and q['after']=='correct' for q in measurements)
assert not regressions
assert review['candidate']['reviewed_counts']['correct']==90
assert product['baseline']['epoch_id']==product['candidate']['epoch_id']
assert product['baseline']['fixture_sha256']==product['candidate']['fixture_sha256']
assert product['baseline']['load_receipt_sha256']==product['candidate']['load_receipt_sha256']=='0db09445eb65d8957bc8dbb14490cef96bc7d14b3845a6ff2a40a98f8de4720f'
assert all(len(r['tables'])==24 and len(r['queries'])==103 and r['stopped'] for r in product.values())
result=dict(status='PASS_ISSUE_199',overall_sf1_status='INCOMPLETE',scope='EQ02 retained SF1 engineering correction; SP frozen',
            sail_commit='528b49dac7beafa6d5a0e1f2e538efcbbc4aca2f',
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
            reduced_cases=47,reduced_completed_cases=43,reduced_expected_cast_failures=4,pytest_cases_per_engine=52,pytest_engines=['Spark 4.2.0 JVM','Sail local','Sail local-cluster'],rust_regressions=5,
            cleanup=cleanup,remaining_issues=[197,198])
first=read(root/'candidate-first/product/result.json')
first_ledger=read(root/'candidate-first/comparison.json')
first_status={q['id']:q['status'] for q in first_ledger['queries']}
assert all(first_status[q]=='correct' for q in ids)
assert first['epoch_id']==product['candidate']['epoch_id']
first_failures=sorted(q for q in first_status if first_status[q]=='failed' and status['baseline'][q]!='failed')
assert first_failures==['q1','q32','q92']
result['rejected_first_candidate']=dict(source=first['sail_commit'],release_identity=first['release_identity'],
    raw_counts=first_ledger['counts'],introduced_failures=first_failures,
    reason='Decimal UDF rejected optimizer-generated untyped NULL while decorrelating aggregate subqueries',
    reduced_reproduction='Five correlated-aggregate tests fail on first candidate and pass on Spark and final candidate',
    cleanup=read(root/'candidate-first/verify-cleanup.json'),
    elapsed_seconds=first['elapsed_seconds'])
result['full_run_seconds']={mode:r['elapsed_seconds'] for mode,r in product.items()}
result['rust_validation']=dict(arithmetic_source=result['sail_commit'],arithmetic_checks=3,
    codec_source='0c5b0bceeeceed605db6f45f523c6bc5d38b966b',codec_checks=2,
    note='Codec implementation unchanged by null-coercion follow-up; final installed local-cluster SQL suite also passes')
(local/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ['status','reviewed_counts','affected_query_seconds','new_regressions']},indent=2))
