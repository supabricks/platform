import collections,itertools,json,math,struct
from pathlib import Path
p=Path('/data2/supabricks-eq/eq206/diagnostics/oracle.json');oracle=json.loads(p.read_text());groups={tuple(c['key']):c for c in oracle['cells']}
def update(values):
 n=mean=m2=0.
 for v in values:
  n+=1;d=v-mean;dn=d/n;mean+=dn;m2+=d*(d-dn)
 return n,mean,m2
def merge(a,b):
 n=a[0]+b[0];delta=b[1]-a[1];dn=delta/n if n else 0.
 return n,a[1]+dn*b[0],a[2]+b[2]+delta*dn*a[0]*b[0]
def bits(v):return struct.unpack('>Q',struct.pack('>d',v))[0]
results=[]
for key,c in groups.items():
 vals=[v for _,v in c['moments']['rows'] if v is not None];assert 2 <= len(vals) <= 4
 outputs=set();trials=0
 for permutation in itertools.permutations(vals):
  for cutmask in range(1<<(len(vals)-1)):
   chunks=[];start=0
   for i in range(1,len(vals)):
    if cutmask & (1<<(i-1)):chunks.append(permutation[start:i]);start=i
   chunks.append(permutation[start:]);state=(0.,0.,0.)
   for chunk in chunks:state=merge(state,update(chunk))
   outputs.add(math.sqrt(state[2]/(state[0]-1))/(sum(vals)/len(vals)));trials+=1
 maxulp=max(abs(bits(v)-bits(c['nearest_double'])) for v in outputs)
 results.append(dict(key=key,update_merge_arrangements=trials,distinct_outputs=len(outputs),minimum=min(outputs),maximum=max(outputs),max_oracle_ulp=maxulp))
report=dict(scope='All nonnull-value permutations and nonempty contiguous partitionings per group (two to four nonnull values), Spark update and sequential final merge into a zero buffer. This is a finite SF1 sensitivity check, not a universal floating-point error theorem.',max_oracle_ulp=max(x['max_oracle_ulp'] for x in results),groups=results)
Path('/data2/supabricks-eq/eq206/diagnostics/order-envelope.json').write_text(json.dumps(report,indent=2)+'\n');print('maximum ULP',report['max_oracle_ulp']);print(collections.Counter(x['max_oracle_ulp'] for x in results))
