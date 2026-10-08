"""Post-run monotonic observation bounds for this insert-only, single-writer load."""
import argparse,json,math,hashlib
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--load',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
r=json.loads((a.load/'result.json').read_text());assert r['status']!='RUNNING' and r['stopped']
obs=[json.loads(s) for s in (a.load/'observations.jsonl').read_text().splitlines()]
obs=[(d['elapsed_seconds'],d['publication']['rows']) for d in obs if d.get('publication')]
assert all(x[0]<y[0] and x[1]<=y[1] for x,y in zip(obs,obs[1:]))
acks=[json.loads(s) for s in (a.load/'commits.jsonl').read_text().splitlines() if json.loads(s)['kind']=='ack']
assert len(acks)==r['committed_transactions'] and sum(d['rows'] for d in acks)==r['committed_rows']
lo=[];hi=[];j=rows=covered_rows=0
for d in acks:
 rows+=d['rows']
 while j<len(obs) and obs[j][1]<rows:j+=1
 if j==len(obs):continue
 # Observation timestamps precede their synchronous RPCs. The next observation
 # start is a safe upper bound on the preceding RPC completion. Final elapsed
 # includes cleanup, so the last bracket is deliberately more conservative.
 lower=max(0,(obs[j-1][0] if j else 0)-d['elapsed_seconds'])
 upper=(obs[j+1][0] if j+1<len(obs) else r['elapsed_seconds'])-d['elapsed_seconds']
 assert 0<=lower<=upper
 lo.append(lower*1000);hi.append(upper*1000);covered_rows+=d['rows']
def stats(v):
 v=sorted(v)
 return {k:round(v[max(0,math.ceil(q*len(v))-1)],3) for k,q in [('p50',.5),('p95',.95),('p99',.99),('max',1)]} if v else {}
if r['status']=='PASS':assert len(hi)==len(acks)
result=dict(status=r['status'],scope='Transaction-weighted commit acknowledgement to publication observation bounds. Only valid for this empty-baseline insert-only single-writer load. Observation starts precede RPCs, so upper bounds use the next observation start; the final bound includes cleanup. Not exact publication timestamps or a sustained 50-row/s SLO.',
 total_transactions=len(acks),covered_transactions=len(hi),unpublished_transactions=len(acks)-len(hi),covered_rows=covered_rows,unpublished_rows=rows-covered_rows,
 lower_bound_ms=stats(lo),upper_bound_ms=stats(hi),source_sha256={n:hashlib.sha256((a.load/n).read_bytes()).hexdigest() for n in ['result.json','commits.jsonl','observations.jsonl']})
a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
