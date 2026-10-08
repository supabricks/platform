import collections,decimal,itertools,json,math,struct
from pathlib import Path
root=Path('/data2/supabricks-eq/eq206/diagnostics');groups=json.loads((root/'groups.json').read_text())['groups']
ref=[json.loads(l) for l in Path('/data2/supabricks-eq/eq02-20261007/reference-02/queries/q39a.rows.jsonl').read_text().splitlines()]
refs={tuple(map(int,[r[o],r[o+1],r[o+2]])):float(r[o+4]) for r in ref for o in [0,5]}
def model(vals,spark):
 n=mean=m2=0.
 for v in vals:
  n+=1;delta=v-mean;dn=delta/n;newmean=mean+dn
  m2+=delta*(delta-dn if spark else v-newmean);mean=newmean
 return math.sqrt(m2/(n-1))/(sum(vals)/n)
def bits(f):return struct.unpack('>Q',struct.pack('>d',f))[0]
counts=collections.Counter();out=[]
for g in groups:
 vals=[v for _,v in g['rows'] if v is not None];expected=refs[tuple(g['key'])]
 with decimal.localcontext() as c:
  c.prec=80;vs=list(map(decimal.Decimal,vals));avg=sum(vs)/len(vs);variance=sum((v-avg)**2 for v in vs)/(len(vs)-1);exact=variance.sqrt()/avg
 direct={name:model(vals,is_spark) for name,is_spark in [('spark',True),('datafusion',False)]}
 possible={name:sorted({model(p,is_spark) for p in itertools.permutations(vals)}) for name,is_spark in [('spark',True),('datafusion',False)]}
 for name in direct:
  counts[name+'_date_order_matches']+=direct[name]==expected
  counts[name+'_some_order_matches']+=expected in possible[name]
  counts[name+'_order_sensitive']+=len(possible[name])>1
 out.append(dict(**g,reference=expected,decimal80_cov=str(exact),reference_oracle_ulp=abs(bits(expected)-bits(float(exact))),date_order=direct,permutations=possible))
(root/'model.json').write_text(json.dumps(dict(counts=counts,groups=out),indent=2)+'\n');print(json.dumps(counts));print('reference vs oracle ULP',collections.Counter(g['reference_oracle_ulp'] for g in out));print('discriminating',[g for g in out if g['date_order']['spark']!=g['date_order']['datafusion']][:2])
