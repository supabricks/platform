"""Read-only EQ220 receipts; tail uses actual publication boundaries, lag is sampled upper bound."""
import bisect,json,sqlite3,sys
from pathlib import Path
from statistics import median

def quantiles(values):
    values=sorted(values)
    return {k:values[min(len(values)-1,int((len(values)-1)*p))] for k,p in [('p50',.5),('p95',.95),('p99',.99),('maximum',1)]}

def summarize(root):
    report=json.loads((root/'result.json').read_text())
    assert report['status']=='PREFIX_PASS', report['status']
    db=sqlite3.connect((root/'state/state.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON')
    pubs=[]
    for at,raw in db.execute("select published_at_ms,descriptor from publications where state='published' order by ordinal"):
        d=json.loads(raw);m=d['manifest'];pubs.append(dict(at_ms=at,rows=sum(t['rows'] for t in m['tables']),bytes=sum(f['bytes'] for f in m['files']),epoch=d['epoch_id']))
    db.close()
    assert pubs[-1]['rows']==report['committed_rows']==report['load_rows']
    # Fixed degraded-scale cohort chosen before the remaining candidates run.
    start=next(p for p in pubs if p['rows']>=13000000)
    end=next(p for p in pubs if p['rows']>=14600000)
    tail=dict(first=start,last=end,rows=end['rows']-start['rows'],seconds=(end['at_ms']-start['at_ms'])/1000)
    tail['rows_per_second']=tail['rows']/tail['seconds']
    observations=[json.loads(line) for line in (root/'observations.jsonl').read_text().splitlines()]
    observed=[((o.get('publication') or {}).get('rows',0),o['elapsed_seconds']) for o in observations]
    assert all(a[0]<=b[0] for a,b in zip(observed,observed[1:]))
    # The drain predicate fetches current() after sample(); a final publication
    # can land between those reads. The completed result records that exact
    # final descriptor. Use its checkpoint time as a conservative observation
    # bound (including shutdown), never invent an earlier publication time.
    final_rows=sum(t['rows'] for t in report['publication']['descriptor']['manifest']['tables'])
    assert final_rows==report['committed_rows']
    if observed[-1][0]<final_rows:observed.append((final_rows,report['elapsed_seconds']))
    counts=[n for n,t in observed];lags=[];rows=0;copy=[]
    for line in (root/'commits.jsonl').read_text().splitlines():
        ack=json.loads(line)
        if ack['kind']!='ack':continue
        rows+=ack['rows'];index=bisect.bisect_left(counts,rows)
        assert index<len(observed)
        lags.append(max(0,observed[index][1]-ack['elapsed_seconds']))
        copy.append(ack['copy_and_commit_ms'])
    assert rows==report['load_rows']
    resources=[json.loads(line) for line in (root.parent/(root.name+'-control')/'resources.jsonl').read_text().splitlines()]
    competing=[s for s in resources if s.get('observed_compilers')]
    return dict(release_identity=report['release_identity'],workload_profile_sha256=report['workload_profile_sha256'],fixture_sha256=report['fixture_sha256'],rows=rows,load_seconds=report['load_seconds'],drain_seconds=report['drain_seconds'],whole_prefix_rows_per_second=rows/(report['load_seconds']+report['drain_seconds']),tail=tail,sampled_commit_to_publication_upper_bound_seconds=quantiles(lags),copy_commit_ms=quantiles(copy),maximum_observed_backlog_rows=max(o['committed_rows']-(o.get('publication') or {}).get('rows',0) for o in observations),peak_cgroup_memory_bytes=max(int(s.get('memory.current',0)) for s in resources),compiler_observation_samples=len(competing),peak_sampled_cell_bytes=report['peak_sampled_cell_bytes'],qualification='prefix only; sampled lag includes observation latency; cohort endpoints rounded up to actual publications')

if __name__=='__main__':
    root=Path(sys.argv[1]);out=Path(sys.argv[2]);out.write_text(json.dumps(summarize(root),indent=2)+'\n')
