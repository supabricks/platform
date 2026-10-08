import argparse,collections,decimal,hashlib,json,re,sys
from pathlib import Path
sys.path.insert(0,'e2e/tpcds')
from inputs import sha,statements
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
root=a.root;reference=Path('/data2/supabricks-eq/eq02-20261007/reference-02');inputs=Path('build/eq00-20261006/inputs/spark')
product=root/'product';report=json.loads((root/'comparison.json').read_text())
a={q['id']:q for q in json.loads((product/'result.json').read_text())['queries']};b={q['id']:q for q in json.loads((reference/'result.json').read_text())['queries']}
def rows(base,q):
 path=base/'queries'/(q['id']+'.rows.jsonl');assert sha(path)==q['result_sha256'];return [json.loads(l) for l in path.read_text().splitlines()]
def canonical(r):return json.dumps(r,separators=(',',':'),ensure_ascii=True)
def nk(x,number=False):return (x is not None,decimal.Decimal(x) if x is not None and number else x)
keys={'q75':lambda r:(nk(r[8],True),),'q24a':None,'q56':lambda r:(nk(r[1],True),),'q64':lambda r:(nk(r[0]),nk(r[1]),nk(r[20],True)),'q77':lambda r:(nk(r[0]),nk(r[1],True)),'q79':lambda r:(nk(r[0]),nk(r[1]),nk(r[2]),nk(r[5],True))}
clauses={'q75':'ORDER BY sales_cnt_diff LIMIT 100; identical selected typed rows and sorted difference keys; only ties reorder.','q24a':'No ORDER BY or LIMIT; exact typed multiset.','q56':'ORDER BY total_sales LIMIT 100; same selected rows and sorted keys.','q64':'ORDER BY cs1.product_name, cs1.store_name, cs2.cnt; same rows and sorted keys.','q77':'ORDER BY channel, id LIMIT 100; same selected rows and sorted keys, NULLS FIRST.','q79':'ORDER BY c_last_name, c_first_name, substr(s_city,1,30), profit LIMIT 100; same selected rows and sorted keys.'}
reviews=[];mismatches=[];failures=[];unreviewed=[]
for q in report['queries']:
 k=q['id'];sql=statements((inputs/(k+'.sql')).read_text())[0];assert hashlib.sha256(sql.encode()).hexdigest()==q['sql_sha256']
 if q['status']=='order_review_required':
  if k not in keys:unreviewed.append(k);continue
  av=rows(product,a[k]);bv=rows(reference,b[k]);key=keys[k]
  assert [f['type'] for f in a[k]['schema']['fields']]==[f['type'] for f in b[k]['schema']['fields']]
  assert collections.Counter(map(canonical,av))==collections.Counter(map(canonical,bv))
  if key:
   ak=list(map(key,av));bk=list(map(key,bv));assert ak==sorted(ak) and bk==sorted(bk) and ak==bk
  else:assert not re.search(r'\border\s+by\b|\blimit\b',sql,re.I)
  reviews.append(dict(id=k,sql_sha256=q['sql_sha256'],status='correct_after_explicit_order_review',reason=clauses[k],product_sha256=a[k]['result_sha256'],reference_sha256=b[k]['result_sha256'],rows=len(av)))
 elif q['status']=='result_mismatch_review_required':
  av=rows(product,a[k]);bv=rows(reference,b[k]);differences=[]
  for i,(x,y) in enumerate(zip(av,bv)):
   for j,(v,w) in enumerate(zip(x,y)):
    if v!=w:differences.append(dict(row=i,column=j,type=a[k]['schema']['fields'][j]['type'],product=v,reference=w))
  mismatches.append(dict(id=k,product_rows=len(av),reference_rows=len(bv),different_values=len(differences),differences=differences))
 elif q['status'] not in ('correct','not_run'):
  path=product/'queries'/(k+'.error.txt');message=path.read_text().splitlines()[-1] if path.exists() else a[k].get('reason')
  issue=197 if message and 'total memory pool' in message else 198 if message and 'No field named' in message else None
  failures.append(dict(id=k,issue=issue,status=q['status'],error=message))
result=dict(status='INCOMPLETE',fixture_sha256=sha(Path(__file__)),comparison_sha256=sha(root/'comparison.json'),raw_counts=report['counts'],reviewed_counts=dict(correct=report['counts'].get('correct',0)+len(reviews),value_mismatch=len(mismatches),execution_failed=len(failures),order_review_pending=len(unreviewed)),order_reviews=reviews,value_differences=mismatches,execution_failures=failures,order_review_pending=unreviewed,scope='Strict comparator unchanged; only individually reviewed SQL ordering cases admitted. No numeric tolerance or rewritten query.')
(root/'review.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['reviewed_counts']))
