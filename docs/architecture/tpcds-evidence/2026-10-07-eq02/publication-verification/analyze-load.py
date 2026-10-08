import argparse, collections, gzip, json, sqlite3
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--load',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
report=json.loads((a.load/'result.json').read_text())
c=sqlite3.connect('file:'+str(a.load/'state/state.sqlite3')+'?mode=ro',uri=True)
rows=[]
for requested,published,raw in c.execute("select requested_at_ms,published_at_ms,descriptor from publications where state='published' order by ordinal"):
 d=json.loads(raw);m=d['manifest']
 rows.append(dict(requested_at_ms=requested,published_at_ms=published,prepared_at_ms=d['prepared_at_ms'],request_to_publication_ms=published-requested,prepared_to_publication_ms=published-d['prepared_at_ms'],rows=sum(t['rows'] for t in m['tables']),generation_bytes=m.get('generation_bytes'),files=len(m['files']),apply_metrics=m.get('apply_metrics',[]),compaction=m.get('compaction')))
merges=collections.Counter()
for r in rows:
 for m in r['apply_metrics']:
  for k,v in m['metrics'].items():
   if type(v) in (float,int):merges[k]+=v
summary=dict(scope='Retained receipts; request-to-publication includes worker and publication, merge/compaction are nested worker costs. No component CPU attribution from wall-clock intervals.',status=report['status'],release_identity=report['release_identity'],committed_rows=report['committed_rows'],published_rows=rows[-1]['rows'],elapsed_seconds=report['elapsed_seconds'],flow_control_wait_seconds=report['flow_control_wait_seconds'],published_epochs=len(rows),request_to_publication_ms=sum(r['request_to_publication_ms'] for r in rows),prepared_to_publication_ms=sum(r['prepared_to_publication_ms'] for r in rows),merge_metrics=dict(merges),compactions=sum(bool(r['compaction']) for r in rows),compaction_ms=sum(r['compaction']['elapsed_ms'] for r in rows if r['compaction']),last_publication=rows[-1])
(a.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
with gzip.GzipFile(filename=str(a.output/'publication-timings.json.gz'),mode='wb',mtime=0) as f:f.write((json.dumps(rows)+'\n').encode())
print(json.dumps(summary,indent=2))
