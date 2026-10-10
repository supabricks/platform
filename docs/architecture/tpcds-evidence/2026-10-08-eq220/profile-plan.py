import cProfile,copy,hashlib,json,pstats,sys,time
from pathlib import Path
release=Path(sys.argv[1]);state=Path(sys.argv[2]);clone=Path(sys.argv[3]);output=Path(sys.argv[4]);output.mkdir(exist_ok=False)
sys.path.insert(0,str(release/'python/analytics'))
import incremental_worker as w
from incremental.planning import mutation_lease
from incremental.storage import journal,verified_digests
config=json.loads(next((state/'analytics/apply-workers').glob('*/input.json')).read_text())
config.pop('journal_access',None);config['spool']='/data2/supabricks-eq/eq220/plan-spool-02/spool.sqlite3';config['deadline_ms']=int(time.time()*1000)+120000
# The paused source journal is accessed through its existing read-only backend.
# No acknowledgment, source compute, worker write or publication is performed.
data=journal(config);previous=config['previous']
metrics=[]
for index in range(3):
 start=time.monotonic()
 with mutation_lease(clone) as lease,verified_digests(lease,config):plan=w.plan(config,clone,previous,data,lease)
 metrics.append(time.monotonic()-start)
prof=cProfile.Profile()
with mutation_lease(clone) as lease,verified_digests(lease,config):
 prof.runcall(w.plan,config,clone,previous,data,lease)
prof.dump_stats(str(output/'plan.prof'))
with (output/'plan.txt').open('w') as f:pstats.Stats(prof,stream=f).strip_dirs().sort_stats('cumulative').print_stats(45)
(output/'result.json').write_text(json.dumps(dict(scope='Isolated read-only planning on cloned paused SF100 prefix; profile overhead excluded from unprofiled times',release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),transactions=len(data[1]),journal_read=config['_journal_read'],unprofiled_plan_seconds=metrics,planned_tables=[dict(oid=t['oid'],rows=len(t['rows'])) for t in plan['tables']]),indent=2)+'\n')
