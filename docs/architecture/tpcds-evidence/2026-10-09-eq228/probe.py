import gc,hashlib,json,os,resource,subprocess,sys,time
from pathlib import Path
release=Path(sys.argv[1]).resolve();fixture=Path(sys.argv[2]);out=Path(sys.argv[3]);mode=sys.argv[4];out.mkdir(mode=0o700)
os.umask(0o077);sys.path.insert(0,os.environ.get('EQ228_SOURCE',str(release/'python/analytics')))
import incremental_worker as w
import incremental.storage as s
from incremental.planning import mutation_lease
config=json.loads((fixture/'config.json').read_text());meta=json.loads((fixture/'source.json').read_text());root=out/'roots'/config['identity']['generation'];root.parent.mkdir(mode=0o700)
subprocess.run(['cp','-a','--reflink=auto',meta['root'],str(root)],check=True)
keep={f['path'] for f in config['previous']['manifest']['files']}|{'owner.json'}
for p in root.rglob('*'):
 if p.is_file() and str(p.relative_to(root)) not in keep:p.unlink()
work=out/'work';work.mkdir(mode=0o700);config.update(generation=str(root),previous_generation=str(root),workspace=str(work),deadline_ms=int(time.time()*1000)+300000)
events=[]
def mem():
 d=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
 return dict(rss=int(d['VmRSS'].split()[0])*1024,peak=int(d['VmHWM'].split()[0])*1024,arrow=w.pa.total_allocated_bytes())
def wrap(module,name):
 original=getattr(module,name)
 def call(*a,**kw):
  before=mem();start=time.perf_counter();cpu=time.process_time()
  try:return original(*a,**kw)
  finally:events.append(dict(stage=name,wall=time.perf_counter()-start,cpu=time.process_time()-cpu,before=before,after=mem()))
 setattr(module,name,call)
for name in ('journal','initialize','verify_previous','decode','overlay','plan','schema_for','apply_table','inventory','durable','run'):wrap(w,name)
wrap(w.DeltaTable,'to_pyarrow_dataset')
original_batches=w.key_batches
def batches(*a,**kw):
 before=mem();start=time.perf_counter();count=0
 for batch in original_batches(*a,**kw):
  count+=batch.num_rows
  yield batch
 events.append(dict(stage='key_batches',wall=time.perf_counter()-start,before=before,after=mem(),rows=count))
w.key_batches=batches
hashes=dict(calls=0,misses=0,bytes=0,seconds=0)
digest=s.digest
def hashed(path):
 path=Path(path);old=s._hashes.get(path);hit=old is not None and old[0]==s._file_stamp(path.stat());start=time.perf_counter()
 try:return digest(path)
 finally:
  hashes['calls']+=1;hashes['misses']+=int(not hit);hashes['bytes']+=0 if hit else path.stat().st_size;hashes['seconds']+=time.perf_counter()-start
s.digest=hashed
if mode=='warm':
 with mutation_lease(root) as lease,s.verified_digests(lease,config):s.verify_previous(root,config['previous'])
 hashes=dict(calls=0,misses=0,bytes=0,seconds=0)
from incremental.reuse import serve
mailbox=out/'input.json';s.atomic(mailbox,config)
completed={}
def execute(c):
 start=time.perf_counter();w.run(c);completed['seconds']=time.perf_counter()-start;gc.collect();completed['post_gc']=mem();completed['ru_maxrss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024;return 0
serve(mailbox,execute)
elapsed=completed['seconds'];post=completed['post_gc'];done=json.loads((out/'done.json').read_text())
receipt=json.loads((work/'result.json').read_text());assert receipt['state']=='ready'
(out/'profile.json').write_text(json.dumps(dict(mode=mode,source_revision=os.environ.get('EQ228_SOURCE','installed'),release_sha256=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256((fixture/'config.json').read_bytes()).hexdigest(),plan_sha256=hashlib.sha256((work/'plan.json').read_bytes()).hexdigest(),scope=meta['scope'],done=done,ru_maxrss_bytes=completed['ru_maxrss_bytes'],seconds=elapsed,post_gc=post,recycle_peak=post['peak']>=512*1024**2,hashes=hashes,events=events,metrics=receipt['descriptor']['manifest']['apply_metrics']),indent=2))
print(json.dumps(dict(output=str(out),seconds=elapsed,post_gc=post,hashes=hashes)))
