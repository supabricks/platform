import collections,decimal,hashlib,json,struct
from pathlib import Path
root=Path('/data2/supabricks-eq/eq206/diagnostics');source=root/'all-groups.json'
groups=json.loads(source.read_text())['groups'];stats={}
for g in groups:
 vals=[v for _,v in g['rows'] if v is not None];assert all(isinstance(v,int) and v>=0 for v in vals)
 n=len(vals);s=sum(vals);ss=sum(v*v for v in vals)
 if n<2 or s==0:continue
 # CV^2 = n * (n*sum(x^2)-sum(x)^2) / ((n-1)*sum(x)^2).
 # Membership comparisons use integer arithmetic, including 1.5^2=9/4.
 num=n*(n*ss-s*s);den=(n-1)*s*s
 stats[tuple(g['key'])]=dict(n=n,sum=s,sum_squares=ss,num=num,den=den,rows=g['rows'])
pairs={}
for q in ['q39a','q39b']:
 selected=[]
 for (w,i,m),a in stats.items():
  b=stats.get((w,i,2))
  if m!=1 or b is None or a['num']<=a['den'] or b['num']<=b['den']:continue
  if q=='q39b' and 4*a['num']<=9*a['den']:continue
  selected.append((w,i))
 pairs[q]=sorted(selected)
print('exact row identities', {q:len(v) for q,v in pairs.items()})
refroot=Path('/data2/supabricks-eq/eq02-20261007/reference-02/queries')
rowsout=[]
def bits(x):return struct.unpack('>Q',struct.pack('>d',x))[0]
for q,keys in pairs.items():
 reference=[json.loads(l) for l in (refroot/(q+'.rows.jsonl')).read_text().splitlines()]
 assert [(int(r[0]),int(r[1])) for r in reference]==keys,q
 for index,row in enumerate(reference):
  for offset in [0,5]:
   key=tuple(map(int,row[offset:offset+3]));s=stats[key]
   assert int(row[offset+2])==(1 if offset==0 else 2)
   assert float(row[offset+3])==s['sum']/s['n']
   with decimal.localcontext() as ctx:
    ctx.prec=80;value=(decimal.Decimal(s['num'])/s['den']).sqrt()
   with decimal.localcontext() as ctx:
    ctx.prec=160;check=(decimal.Decimal(s['num'])/s['den']).sqrt()
   assert float(value)==float(check)
   rowsout.append(dict(query=q,row=index,column=offset+4,key=key,decimal80=str(value),nearest_double=float(value),spark=float(row[offset+4]),spark_ulp=abs(bits(float(row[offset+4]))-bits(float(value))),moments=s))
result=dict(scope='Independent exact-integer row membership and 80-digit Decimal CV oracle; no comparator acceptance change',fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),group_count=len(groups),input_rows=sum(len(g['rows']) for g in groups),selected_rows={q:len(v) for q,v in pairs.items()},spark_oracle_ulps=dict(collections.Counter(r['spark_ulp'] for r in rowsout)),cells=rowsout)
(root/'oracle.json').write_text(json.dumps(result,indent=2)+'\n');print(result['spark_oracle_ulps'])
