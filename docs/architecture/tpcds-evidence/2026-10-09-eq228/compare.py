"""Compare completed cells only; no overlapping phase sums or sampled-CPU claims."""
import json,statistics
from pathlib import Path
root=Path('<evidence-root>/eq220');output={}
for mode,label in [('baseline','c228b'),('candidate','c228a')]:
 summary=json.loads(Path(f'build/eq228/{mode}-summary.json').read_text());cost=json.loads(Path(f'build/eq228/{mode}-batch-costs.json').read_text())
 lo=summary['tail']['first']['at_ms'];hi=summary['tail']['last']['at_ms'];batches=[b for b in cost['batches'] if lo<b['published_ms']<=hi]
 assert sum(b['rows'] for b in batches)==summary['tail']['rows']
 samples=[json.loads(s) for s in (root/(label+'-control')/'resources.jsonl').read_text().splitlines()]
 processes={}
 for s in samples:
  if not 13000000<=(s.get('published_rows') or 0)<=14600000:continue
  for p in s.get('processes',[]):
   if p['role']!='incremental_worker.py':continue
   key=f"{p['pid']}:{p['start_ticks']}";entry=processes.setdefault(key,dict(first=s['utc'],last=s['utc'],peak_rss_bytes=0));entry['last']=s['utc'];entry['peak_rss_bytes']=max(entry['peak_rss_bytes'],p['rss_bytes'])
 output[mode]=dict(label=label,release_identity=summary['release_identity'],rows=summary['rows'],whole_rows_per_second=summary['whole_prefix_rows_per_second'],late=summary['tail'],lag=summary['sampled_commit_to_publication_upper_bound_seconds'],last30=cost['summaries']['last_30'],late_batches=len(batches),late_medians={k:statistics.median(b[k] for b in batches) for k in ('worker_ms','after_manifest_ms','cycle_ms','rows','journal_ms','delta_execution_ms')},sampled_distinct_late_workers=len(processes),process_samples=processes,observed_compiler_samples=summary['compiler_observation_samples'])
output.update(scope='One matched installed pair; exact verification separate. Process samples use 2-second observed row endpoints; may miss process tails. Nested phases are not additive.',whole_speedup=output['candidate']['whole_rows_per_second']/output['baseline']['whole_rows_per_second'],late_speedup=output['candidate']['late']['rows_per_second']/output['baseline']['late']['rows_per_second'])
Path('build/eq228/installed-comparison.json').write_text(json.dumps(output,indent=2)+'\n')
print(json.dumps({k:v for k,v in output.items() if k not in ('candidate','baseline')},indent=2))
