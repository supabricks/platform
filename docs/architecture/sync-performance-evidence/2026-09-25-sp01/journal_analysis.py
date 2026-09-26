"""Derive SP01 retry evidence from every accepted profiled trial, without dropping failures."""
import collections,gzip,json,statistics,sys
from pathlib import Path

def spread(values):
    return dict(n=len(values),median=statistics.median(values) if values else None,min=min(values) if values else None,max=max(values) if values else None)

def analyze(root):
    experiment=json.loads((root/'experiment.json').read_text());trials=[]
    for pair in experiment['pairs']:
        for arm,result in pair['results'].items():
            profiles=list((root/result['directory']).rglob('profile.json.gz'))
            if not profiles:continue
            assert len(profiles)==1
            profile=json.loads(gzip.decompress(profiles[0].read_bytes()));batches=profile['batches']
            available=any('journal_reads' in batch for batch in batches)
            reads=[r for batch in batches for r in batch.get('journal_reads',[])]
            worker_reads=[];busy=[]
            for name,rows in profile['workers'].items():
                if not name.startswith('incremental-'):continue
                last=rows[-1];read=last.get('metrics',{}).get('apply.journal')
                if read and read['calls']:
                    worker_reads.append(dict(stream=name,context_id=last['context_id'],final=last['final'],milliseconds=read['total_ns']/1e6,errors=read.get('errors',0)))
                busy.extend(e for e in last.get('exceptions',[]) if e.get('stage')=='sqlite.SELECT' and e.get('sqlite_errorcode') in (5,261,517,773))
            trials.append(dict(arm=arm,**{k:pair['pair'][k] for k in ('cpus','rate','repeat')},directory=result['directory'],status=result['metrics']['status'],runtime_error=result['metrics'].get('runtime_error'),
                scope='whole fixture: bootstrap, warmup, measured load and drain; receipt totals are not a steady-state rate',
                receipt_counters_available=available,batches=len(batches),batch_states=dict(collections.Counter(b['state'] for b in batches)),batch_errors=dict(collections.Counter(b['error'] for b in batches if b.get('error'))),
                worker_attempts=sum(b.get('attempts',0) for b in batches) if available else None,
                journal_deferrals=sum(b.get('journal_deferrals',0) for b in batches) if available else None,
                reads=len(reads) if available else None,read_outcomes=dict(collections.Counter(r['outcome'] for r in reads)) if available else None,
                busy_errors=sum(r['busy'] for r in reads) if available else None,
                read_attempts=sum(r['attempts'] for r in reads) if available else None,
                retried_complete_reads=sum(r['outcome']=='complete' and r['busy']>0 for r in reads) if available else None,
                elapsed_ms=spread([r['elapsed_ms'] for r in reads]) if available else None,
                backoff_ms=sum(r['wait_ms'] for r in reads) if available else None,
                profiled_read_workers=worker_reads,profiled_sqlite_select_busy_events=len(busy)))
    groups=[]
    for cpu,rate in sorted({(r['cpus'],r['rate']) for r in trials}):
        for arm in ('predecessor','candidate'):
            rows=[r for r in trials if (r['cpus'],r['rate'],r['arm'])==(cpu,rate,arm)]
            if not rows:continue
            groups.append(dict(cpus=cpu,rate=rate,arm=arm,trials=len(rows),**{k:spread([r[k] for r in rows if r[k] is not None]) for k in ('journal_deferrals','busy_errors','retried_complete_reads','backoff_ms')}))
    return dict(scope='Accepted trials only; rejected/contended attempts remain in the immutable experiment archive. Missing predecessor counters remain null, never zero.',groups=groups,trials=trials)

if __name__=='__main__':
    root=Path(sys.argv[1]);(root/'journal-analysis.json').write_text(json.dumps(analyze(root),indent=2,sort_keys=True)+'\n')
