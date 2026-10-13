"""Replay the retained post-compaction apply in an independent copied generation."""
import faulthandler,json,os,resource,shutil,sys,time
from pathlib import Path
os.umask(0o077)
sys.path.insert(0,str(Path(sys.executable).resolve().parents[2]/'analytics'))
import incremental_worker as w
base=Path('/failed');c=json.loads((base/'private-stall/input.json').read_text());plan=json.loads((base/'private-stall/plan.json').read_text())
root=Path('/diag/analytics/incremental/generation');root.parent.mkdir(parents=True)
shutil.copytree('/compacted/analytics/incremental/generation',root)
c['deadline_ms']=int(time.time()*1000)+60000
receipt=json.loads((root/'compaction.json').read_text())
report=dict(status='RUNNING',scope='copied post-compaction apply only; no installed-worker or throughput qualification')
Path('/diag/result.json').write_text(json.dumps(report)+'\n')
faulthandler.dump_traceback_later(10,repeat=True)
mode=sys.argv[1]
report['mode']=mode
if mode in ('sparse','sparse-range','sparse-in'):
 planned=plan['tables'][0];table=next(t for t in receipt['tables'] if str(t['oid'])==planned['oid']);path=root/table['path']
 dataset=w.DeltaTable(str(path)).to_pyarrow_dataset(filesystem=w.fs.SubTreeFileSystem(str(path),w.fs.LocalFileSystem()))
 keys={1,table['rows']};rows=[r for b in w.key_batches(dataset,planned['columns'],keys) for r in b.to_pylist()]
 assert len(rows)==2
 planned['rows']=[[r[planned['columns'][0][1]],[r[col[1]] for col in planned['columns']]] for r in rows]
 planned['rows'][0][1][-1]=99;planned['row_delta']=0
 del dataset,rows
merge=w.DeltaTable.merge
def controlled(self,source,predicate,**kwargs):
 if mode=='streamed':kwargs['streamed_exec']=True
 elif mode=='spill256':kwargs['max_spill_size']=256*1024**2
 elif mode in ('range','sparse-range','sparse-in'):
  planned=plan['tables'][0];pk=w.key_columns(planned['columns'])
  for n,i in enumerate(pk):
   values=[w.key_values(k,pk)[n] for k,_ in planned['rows']]
   field='target.'+w.quote(planned['columns'][i][1])
   predicate+=' AND '+field+' >= '+str(min(values))+' AND '+field+' <= '+str(max(values))
 if mode=='sparse-in':
  field='target.'+w.quote(plan['tables'][0]['columns'][0][1])
  predicate+=' AND '+field+' IN ('+','.join(str(k) for k,_ in plan['tables'][0]['rows'])+')'
 return merge(self,source,predicate,**kwargs)
w.DeltaTable.merge=controlled
start=time.monotonic()
try:
 results=[]
 for planned in plan['tables']:
  table=next(t for t in receipt['tables'] if str(t['oid'])==planned['oid'])
  version,metrics=w.apply_table(c,root,table,planned,w.hashlib.sha256(w.canonical(plan)).hexdigest(),frozenset())
  path=root/table['path']
  filesystem=w.fs.SubTreeFileSystem(str(path),w.fs.LocalFileSystem())
  dataset=w.DeltaTable(str(path),version=version).to_pyarrow_dataset(filesystem=filesystem)
  expected={key:values for key,values in planned['rows'] if values is not None}
  actual={r[planned['columns'][0][1]]:[r[c[1]] for c in planned['columns']] for b in w.key_batches(dataset,planned['columns'],set(expected)) for r in b.to_pylist()}
  assert actual==expected
  count=dataset.count_rows();assert count==table['rows']+planned['row_delta']
  results.append(dict(version=version,metrics=metrics,exact_planned_rows=True,total_rows=count))
 report.update(status='PASS',results=results)
except BaseException as error:report.update(status='FAIL',error=repr(error));raise
finally:
 faulthandler.cancel_dump_traceback_later()
 report.update(elapsed_seconds=time.monotonic()-start,highwater_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)
 Path('/diag/result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
