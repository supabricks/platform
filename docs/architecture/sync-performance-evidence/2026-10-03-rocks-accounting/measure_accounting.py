import hashlib,importlib.util,json,statistics,tempfile,time
from pathlib import Path
import rocks_journal as fixed
root=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('rocks_before',root/'rocks_journal.before.py')
before=importlib.util.module_from_spec(spec);spec.loader.exec_module(before)
result=dict(scope='Hot local filesystem accounting component; not pipeline throughput or sustained-load qualification',files=32,iterations=3000,repeats=3,arms={k:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for k,m in [('before',before),('after',fixed)]},pairs=[])
for repeat in range(3):
 with tempfile.TemporaryDirectory() as tmp:
  path=Path(tmp)
  for i in range(result['files']):(path/f'{i:06}.sst').write_bytes(b'x'*4096)
  values={}
  for name,module in ([('before',before),('after',fixed)] if repeat%2==0 else [('after',fixed),('before',before)]):
   journal=module.RocksJournal.__new__(module.RocksJournal);journal.path=path
   for _ in range(200):journal.sizes()
   cpu=time.process_time();start=time.perf_counter()
   for _ in range(result['iterations']):sizes=journal.sizes()
   elapsed=time.perf_counter()-start;cpu=time.process_time()-cpu
   assert len(sizes)==32 and sum(sizes.values())==32*4096
   values[name]=dict(elapsed_seconds=elapsed,cpu_seconds=cpu,microseconds_per_scan=elapsed*1e6/result['iterations'])
  result['pairs'].append(values)
result['median_us']={name:statistics.median(p[name]['microseconds_per_scan'] for p in result['pairs']) for name in ('before','after')}
result['median_percent']=(result['median_us']['after']/result['median_us']['before']-1)*100
with (root/'accounting-cost.json').open('x') as f:f.write(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
