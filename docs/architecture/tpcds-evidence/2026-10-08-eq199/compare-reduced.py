import hashlib,json
from pathlib import Path
root=Path('/data2/supabricks-eq/eq199')
reference=json.loads((root/'reduced-spark/results.json').read_text())
result={}
for mode in ['baseline','candidate']:
    path=root/('reduced-'+mode)/'results.json';actual=json.loads(path.read_text())
    assert actual['fixture_sha256']==reference['fixture_sha256']
    assert len(actual['queries'])==len(reference['queries'])==47
    checks=[]
    for a,b in zip(actual['queries'],reference['queries']):
        assert (a['id'],a['sql'])==(b['id'],b['sql'])
        if b['status']=='failed':
            # These four deliberately cast an out-of-range integer to DECIMAL(38,38).
            passed=a['status']=='failed' and any(t in a.get('error','').lower() for t in ['overflow','out of range','out_of_range','cannot be represented'])
            checks.append(dict(id=a['id'],expected='cast_overflow',passed=passed))
        else:
            types=lambda q:[f['type'] for f in q.get('schema',{}).get('fields',[])]
            checks.append(dict(id=a['id'],expected='complete',same_types=types(a)==types(b),same_values=a.get('rows')==b['rows'],passed=a['status']=='complete' and types(a)==types(b) and a['rows']==b['rows']))
    result[mode]=dict(passed=sum(q['passed'] for q in checks),total=len(checks),checks=checks,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
(root/'reduced-comparison.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
assert result['candidate']['passed']==47
