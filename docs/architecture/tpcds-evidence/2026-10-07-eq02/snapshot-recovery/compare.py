#!/usr/bin/env python3
"""Conservative full-result ledger; order/tie differences require explicit review."""
import argparse
from collections import Counter
import json
from pathlib import Path

from inputs import sha
from load import save


def disposition(actual, expected):
    # Do not round exact decimals or invent a float tolerance after seeing data.
    # Unnamed expression column names/nullability can vary across engines; compare
    # position and SQL value types, retaining both original schemas for review.
    if [f['type'] for f in actual['schema']['fields']] != [f['type'] for f in expected['schema']['fields']]:
        return 'type_mismatch'
    left, right=actual['values'], expected['values']
    if left==right:return 'correct'
    key=lambda row:json.dumps(row,ensure_ascii=True,separators=(',',':'))
    if Counter(map(key,left))==Counter(map(key,right)):
        return 'order_review_required'
    return 'result_mismatch_review_required'


def run(product, reference, output):
    if output.exists():raise ValueError('fresh comparison output required')
    actual=json.loads((product/'result.json').read_text());expected=json.loads((reference/'result.json').read_text())
    assert actual['input_lock_sha256']==expected['input_lock_sha256']
    assert actual['generation_receipt_sha256']==expected['generation_receipt_sha256']
    assert actual['load_profile_sha256']==expected['load_profile_sha256']
    assert expected['engine']=='Apache Spark JVM' and expected['version']=='4.2.0'
    assert len(actual['tables'])==len(expected['tables'])==24
    assert all(t['status']=='PASS' for t in actual['tables'])
    assert {t['table']:t['rows'] for t in actual['tables']}=={t['name']:t['rows'] for t in expected['tables']}
    assert len(actual['queries'])==len(expected['queries'])==103
    queries=[]
    for a,e in zip(actual['queries'],expected['queries']):
        assert (a['id'],a['sql_sha256'])==(e['id'],e['sql_sha256'])
        entry=dict(id=a['id'],template=a['template'],sql_sha256=a['sql_sha256'],
                   product_status=a['status'],reference_status=e['status'])
        if a['status']!='complete_requires_reference_comparison':entry['status']=a['status']
        elif e['status']!='complete':entry['status']='reference_incomplete'
        else:
            def read(root,q):
                path=root/'queries'/(q['id']+'.rows.jsonl')
                assert sha(path)==q['result_sha256']
                values=[json.loads(line) for line in path.read_text().splitlines()]
                assert len(values)==q['rows']
                return dict(q,values=values)
            entry['status']=disposition(read(product,a),read(reference,e))
        queries.append(entry)
    result=dict(status='PASS' if all(q['status']=='correct' for q in queries) else 'INCOMPLETE',
                product_sha256=sha(product/'result.json'),reference_sha256=sha(reference/'result.json'),
                comparison='strict typed values; no floating tolerance; order/limit ties remain review-required',
                counts=dict(Counter(q['status'] for q in queries)),queries=queries)
    save(output,result)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('product','reference','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();run(args.product,args.reference,args.output)
