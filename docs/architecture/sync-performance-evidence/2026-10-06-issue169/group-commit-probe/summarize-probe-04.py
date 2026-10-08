from pathlib import Path
import json,collections
root=Path(__file__).resolve().parent/'probe-04'
rows=[json.loads(s) for s in (root/'resources.jsonl').read_text().splitlines()]
observed=[r for r in rows if 'pg_waits' in r]
last=observed[-1];start=rows[0]['at_ms']
result=dict(scope='Diagnostic only: additional observations; no causal runtime comparison or qualification',elapsed_seconds=(rows[-1]['at_ms']-start)/1000,source_minutes=[],intervals=[])
for minute,row in last['sql_buckets'].items():
 if row['count']:
  result['source_minutes'].append(dict(minute=int(minute),transactions=row['count'],mean_sql_ms={k:v/row['count'] for k,v in row['sql_total_ms'].items()}))
for seconds in range(0,int(result['elapsed_seconds']),300):
 selected=[r for r in observed if seconds*1000<=r['at_ms']-start<(seconds+300)*1000]
 if len(selected)<2:continue
 a,b=selected[0],selected[-1];duration=(b['at_ms']-a['at_ms'])/1000
 first,lastsk=[r['storage_metrics']['safekeeper'] for r in (a,b)]
 def delta(k):
  value=lastsk[k]-first[k]
  assert value>=0,k
  return value
 calls=delta('safekeeper_flush_wal_seconds_count');waits=collections.Counter()
 for row in selected:
  for w in row['pg_waits']:
   if w['backend_type']=='client backend' and w['state']=='active':waits[(w['wait_type'] or 'running')+'/'+(w['wait'] or 'running')]+=w['count']
 total=sum(waits.values())
 result['intervals'].append(dict(start_seconds=seconds,covered_seconds=duration,flush_mean_ms=1000*delta('safekeeper_flush_wal_seconds_sum')/calls,flush_calls_s=calls/duration,active_wait_counts=waits,active_wait_percent={k:100*v/total for k,v in waits.items()},pg_wal_delta={k:b['pg_wal'][k]-a['pg_wal'][k] for k in a['pg_wal']},pg_tables=b['pg_tables']))
(root/'diagnostic-summary.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(dict(elapsed_seconds=result['elapsed_seconds'],intervals=[{k:v for k,v in r.items() if k in ('start_seconds','covered_seconds','flush_mean_ms','flush_calls_s','active_wait_percent')} for r in result['intervals']]),indent=2))
