import argparse,collections,decimal,hashlib,json,re,sys
from pathlib import Path
sys.path.insert(0,'e2e/tpcds')
from inputs import sha,statements
parser=argparse.ArgumentParser()
parser.add_argument('--root',type=Path,default=Path('/data2/supabricks-eq/eq03-recovery/qualification-01'))
parser.add_argument('--reference',type=Path,default=Path('/data2/supabricks-eq/eq02-20261007/reference-02'))
parser.add_argument('--inputs',type=Path,default=Path('build/eq00-20261006/inputs/spark'))
args=parser.parse_args();root=args.root;reference=args.reference;inputs=args.inputs
product=root/'product';report=json.loads((root/'comparison.json').read_text())
a={q['id']:q for q in json.loads((product/'result.json').read_text())['queries']};b={q['id']:q for q in json.loads((reference/'result.json').read_text())['queries']}
def rows(base,q):
 p=base/'queries'/(q['id']+'.rows.jsonl');assert sha(p)==q['result_sha256']
 return [json.loads(l) for l in p.read_text().splitlines()]
def canonical(r):return json.dumps(r,separators=(',',':'),ensure_ascii=True)
def nullkey(x,number=False):return (x is not None,decimal.Decimal(x) if x is not None and number else x)
order_keys={'q24a':None,'q56':lambda r:(nullkey(r[1],True),),'q64':lambda r:(nullkey(r[0]),nullkey(r[1]),nullkey(r[20],True)),'q77':lambda r:(nullkey(r[0]),nullkey(r[1],True)),'q79':lambda r:(nullkey(r[0]),nullkey(r[1]),nullkey(r[2]),nullkey(r[5],True))}
clauses={'q24a':'No ORDER BY or LIMIT; exact typed row multiset required.','q56':'ORDER BY total_sales LIMIT 100; same selected rows and sorted sales; differences only within ties.','q64':'ORDER BY cs1.product_name, cs1.store_name, cs2.cnt; same rows and sort keys; differences only within ties.','q77':'ORDER BY channel, id LIMIT 100; same selected rows and sort keys, NULLS FIRST; differences only within ties.','q79':'ORDER BY c_last_name, c_first_name, substr(s_city, 1, 30), profit LIMIT 100; same selected rows and all four sort keys; differences only within ties.'}
reviews=[];mismatches=[];failures=[];types=[]
for q in report['queries']:
 k=q['id'];sql=statements((inputs/(k+'.sql')).read_text())[0]
 assert hashlib.sha256(sql.encode()).hexdigest()==q['sql_sha256']
 if q['status']=='order_review_required':
  av=rows(product,a[k]);bv=rows(reference,b[k]);key=order_keys[k]
  assert [f['type'] for f in a[k]['schema']['fields']]==[f['type'] for f in b[k]['schema']['fields']]
  assert collections.Counter(map(canonical,av))==collections.Counter(map(canonical,bv))
  if key:
   ak=list(map(key,av));bk=list(map(key,bv));assert ak==sorted(ak) and bk==sorted(bk) and ak==bk
  else:assert not re.search(r'\border\s+by\b|\blimit\b',sql,re.I)
  reviews.append(dict(id=k,sql_sha256=q['sql_sha256'],status='correct_after_explicit_order_review',reason=clauses[k],product_sha256=a[k]['result_sha256'],reference_sha256=b[k]['result_sha256'],rows=len(av)))
 elif q['status']=='result_mismatch_review_required':
  av=rows(product,a[k]);bv=rows(reference,b[k]);assert len(av)==len(bv)
  differences=[]
  for row,(x,y) in enumerate(zip(av,bv)):
   for col,(v,w) in enumerate(zip(x,y)):
    if v==w:continue
    typ=a[k]['schema']['fields'][col]['type'];match=re.fullmatch(r'decimal\((\d+),(\d+)\)',typ)
    delta=str(decimal.Decimal(v)-decimal.Decimal(w)) if match and v is not None and w is not None else None
    unit=decimal.Decimal(10)**-int(match[2]) if match else None
    differences.append(dict(row=row,column=col,type=typ,product=v,reference=w,delta=delta,one_decimal_unit=delta is not None and abs(decimal.Decimal(delta))==unit))
  mismatches.append(dict(id=k,different_values=len(differences),all_one_decimal_unit=all(d['one_decimal_unit'] for d in differences),differences=differences))
 elif q['status']=='failed':
  message=(product/'queries'/(k+'.error.txt')).read_text().splitlines()[-1]
  issue=197 if 'total memory pool' in message else 198 if 'No field named' in message else None
  assert issue
  failures.append(dict(id=k,issue=issue,error=message))
 elif q['status']=='type_mismatch':
  types.append(dict(id=k,issue=199,fields=[dict(column=i,product=x['type'],reference=y['type']) for i,(x,y) in enumerate(zip(a[k]['schema']['fields'],b[k]['schema']['fields'])) if x['type']!=y['type']]))
result=dict(status='INCOMPLETE',fixture_sha256=sha(Path(__file__)),comparison_sha256=sha(root/'comparison.json'),raw_counts=report['counts'],reviewed_counts=dict(correct=report['counts']['correct']+len(reviews),type_mismatch=len(types),decimal_value_mismatch=len(mismatches),execution_failed=len(failures)),order_reviews=reviews,decimal_value_differences=mismatches,type_differences=types,execution_failures=failures,scope='Strict comparator retained unchanged; only five named SQL ordering cases are explicitly reviewed. No type coercion, numeric tolerance, or rewritten query.')
(root/'review.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(dict(counts=result['reviewed_counts'],decimal_differences=[{k:r[k] for k in ['id','different_values','all_one_decimal_unit']} for r in mismatches],failures=dict(collections.Counter(f['issue'] for f in failures))),indent=2))
