"""Paired real-storage reclamation check; not an end-to-end capacity claim."""
import hashlib,json,shutil,sys,time
from pathlib import Path
root=Path.cwd();sys.path.insert(0,str(root/'python/analytics'))
from capture.spool import Spool,pg_lsn
out=root/'build/issue157-20261005/component-01';out.mkdir()
results=[]
for repeat in range(3):
 for mode in (('prune','maintain') if repeat%2==0 else ('maintain','prune')):
  directory=out/f'{repeat+1}-{mode}';s=Spool(directory,{'decoder_version':1})
  row=dict(repeat=repeat+1,mode=mode,transactions_per_check=625,checks=24,maintenance_ms=[],retained_rows=[])
  started=time.monotonic()
  try:
   s.establish(100,{})
   for tick in range(24):
    for start in range(0,625,32):
     begin=s.captured
     s.append_many([(begin+2*i+1,begin+2*i+2,b'x'*128) for i in range(min(32,625-start))])
    now=time.monotonic();getattr(s,mode)(pg_lsn(s.captured));row['maintenance_ms'].append((time.monotonic()-now)*1000)
    row['retained_rows'].append(s.backend.db.execute('SELECT count(*) FROM transactions').fetchone()[0])
   s.verify();row.update(elapsed_seconds=time.monotonic()-started,physical_bytes=s.physical(),captured=s.captured,storage=s.storage_progress(),status='verified')
  finally:s.close()
  s=Spool(directory,{'decoder_version':1})
  try:s.verify();assert s.captured==30100
  finally:s.close()
  shutil.rmtree(directory);results.append(row)
  (out/'results.json').write_text(json.dumps(dict(scope=__doc__,source_sha256=hashlib.sha256((root/'python/analytics/capture/spool.py').read_bytes()).hexdigest(),results=results),indent=2)+'\n')
  print(mode,repeat+1,row['retained_rows'][-1],row['physical_bytes'],round(max(row['maintenance_ms']),2),flush=True)
