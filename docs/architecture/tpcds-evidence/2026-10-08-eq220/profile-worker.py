"""One installed apply against a private reconstruction of a published prefix."""
import cProfile,copy,hashlib,json,os,pstats,resource,subprocess,sys,time
from pathlib import Path
release=Path(sys.argv[1]).resolve();source=Path(sys.argv[2]);output=Path(sys.argv[3]);output.mkdir(mode=0o700)
os.umask(0o077)
sys.path.insert(0,str(release/'python/analytics'))
import incremental_worker as w
from incremental.planning import mutation_lease
from incremental.storage import verified_digests,verify_previous
original=json.loads(next((source/'analytics/apply-workers').glob('*/input.json')).read_text())
config=copy.deepcopy(original);config.pop('journal_access',None)
config['deadline_ms']=int(time.time()*1000)+300000
config['spool']='/data2/supabricks-eq/eq220/plan-spool-02/spool.sqlite3'
generation=output/'roots'/Path(original['generation']).name;generation.parent.mkdir(mode=0o700)
subprocess.run(['cp','-a','--reflink=auto','/data2/supabricks-eq/eq220/plan-root',str(generation)],check=True)
# Discard the cloned, unacknowledged next Delta commit. Only exact files in the
# published manifest and the ownership marker form this diagnostic baseline.
keep={f['path'] for f in original['previous']['manifest']['files']}|{'owner.json'}
removed=[]
for p in generation.rglob('*'):
 if p.is_file() and str(p.relative_to(generation)) not in keep:
  removed.append(str(p.relative_to(generation)));p.unlink()
config['generation']=config['previous_generation']=str(generation)
work=output/'work';work.mkdir(mode=0o700);config['workspace']=str(work)
# Reused-worker condition: verification of the previous published inventory
# primes bounded content evidence; timing includes the next full apply only.
with mutation_lease(generation) as lease,verified_digests(lease,config):verify_previous(generation,config['previous'])
prof=cProfile.Profile();started=time.monotonic();prof.runcall(w.run,config);elapsed=time.monotonic()-started
prof.dump_stats(str(output/'worker.prof'))
with (output/'worker.txt').open('w') as stream:pstats.Stats(prof,stream=stream).strip_dirs().sort_stats('cumulative').print_stats(65)
receipt=json.loads((work/'result.json').read_text());assert receipt['state']=='ready'
(output/'result.json').write_text(json.dumps(dict(status='PASS',scope='Diagnostic cProfile full apply; private published-prefix reconstruction, direct cloned journal, warmed checksum evidence; no throughput claim',release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),original_request_sha256=hashlib.sha256(json.dumps(original,sort_keys=True).encode()).hexdigest(),removed_unpublished_clone_files=removed,profiled_seconds=elapsed,peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,metrics=receipt['descriptor']['manifest']['apply_metrics']),indent=2)+'\n')
