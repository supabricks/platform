"""Replay the retained post-compaction apply in an independent copied generation."""
import faulthandler,json,os,resource,shutil,sys,time
from pathlib import Path
os.umask(0o077)
sys.path.insert(0,'/repo/build/eq02-20261007/key-pruning-runtime/python/analytics')
import incremental_worker as w
base=Path('/failed');c=json.loads((base/'private-stall/input.json').read_text());plan=json.loads((base/'private-stall/plan.json').read_text())
root=Path('/diag/analytics/incremental/generation');root.parent.mkdir(parents=True)
shutil.copytree('/compacted/analytics/incremental/generation',root)
c['deadline_ms']=int(time.time()*1000)+60000
receipt=json.loads((root/'compaction.json').read_text())
report=dict(status='RUNNING',scope='copied post-compaction apply only; no installed-worker or throughput qualification')
Path('/diag/result.json').write_text(json.dumps(report)+'\n')
faulthandler.dump_traceback_later(10,repeat=True)
start=time.monotonic()
try:
 results=[]
 for planned in plan['tables']:
  table=next(t for t in receipt['tables'] if str(t['oid'])==planned['oid'])
  version,metrics=w.apply_table(c,root,table,planned,w.hashlib.sha256(w.canonical(plan)).hexdigest(),frozenset())
  results.append(dict(version=version,metrics=metrics))
 report.update(status='PASS',results=results)
except BaseException as error:report.update(status='FAIL',error=repr(error));raise
finally:
 faulthandler.cancel_dump_traceback_later()
 report.update(elapsed_seconds=time.monotonic()-start,highwater_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)
 Path('/diag/result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
