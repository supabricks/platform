"""Exact transaction-count attribution; diagnostic timings are nested, not additive."""
import argparse
import bisect
from collections import Counter,defaultdict
import json
from pathlib import Path
import sqlite3
import statistics


def lsn(value):
    if isinstance(value,int):return value
    hi,lo=value.split('/');return int(hi,16)*2**32+int(lo,16)


def read(path):return [json.loads(line) for line in path.read_text().splitlines()]


def stats(values):
    values=sorted(values)
    if not values:return dict(count=0)
    return dict(count=len(values),mean=statistics.mean(values),p50=statistics.median(values),
        p95=values[min(len(values)-1,int((len(values)-1)*.95))],maximum=values[-1],minimum=values[0])


def analyze(root,profile):
    result=json.loads((root/'result.json').read_text());assert result['status']=='PREFIX_PASS'
    events=[];health=[]
    for path in sorted(profile.glob('batches-*.jsonl')):
        rows=read(path);events.extend(rows)
        counters=[r['fields']['observer'] for r in rows if 'observer' in r['fields']]
        assert counters,(path,'missing observer health')
        last=counters[-1];assert last['errors']==last['dropped']==0,(path,last)
        assert path.stat().st_size<8*1024**2-65536,(path,'reserve margin exhausted; terminal counters may be missing')
        health.append(dict(file=path.name,bytes=path.stat().st_size,**{k:v for k,v in last.items() if k!='bytes'}))
    daemon=read(profile/'daemon.jsonl');assert daemon
    last=daemon[-1]
    assert not last['budget_exceeded'] and last['event_dropped']==last['profile_write_errors']==0,last
    events+=read(profile/'daemon-events.jsonl');events.sort(key=lambda r:r['at_ms'])
    durable=[r for r in events if r['stage']=='capture.durable'];transactions=[]
    for row in durable:
        f=row['fields']
        for tx in f['records']:transactions.append(dict(tx,durable_ms=f['durable_ms']))
    transactions.sort(key=lambda t:t['end'])
    assert all(a['end']<b['end'] for a,b in zip(transactions,transactions[1:]))
    positive=[t for t in transactions if t['rows']]
    acks=[a for a in read(root/'commits.jsonl') if a['kind']=='ack']
    assert len(positive)==len(acks),(len(positive),len(acks))
    for index,(tx,ack) in enumerate(zip(positive,acks)):
        assert tx['rows']==ack['rows'],index
        assert lsn(ack['boundary_lsn'])<=tx['commit']<tx['end'],index
        if index+1<len(acks):assert tx['end']<=lsn(acks[index+1]['boundary_lsn']),index
        tx['copy_and_commit_ms']=ack['copy_and_commit_ms'];tx['table']=ack['table'];tx['ack_elapsed_seconds']=ack['elapsed_seconds']
    assert sum(t['rows'] for t in positive)==result['load_rows']
    ends=[t['end'] for t in transactions];cumulative=[0]
    for t in transactions:cumulative.append(cumulative[-1]+t['rows'])
    def rows_to(bound):return cumulative[bisect.bisect_right(ends,lsn(bound))]
    times=[t['durable_ms'] for t in transactions]
    commit_times=[t['committed_at_ms'] for t in transactions]
    assert all(a<=b for a,b in zip(times,times[1:])), 'durable clock reordered'
    assert all(a<=b for a,b in zip(commit_times,commit_times[1:])), 'commit clock reordered'
    def durable_at(at):return cumulative[bisect.bisect_right(times,at)]
    def committed_at(at):return cumulative[bisect.bisect_right(commit_times,at)]
    by_id=defaultdict(lambda:defaultdict(list));targets={}
    for row in events:
        f=row['fields'];key=f.get('id') or f.get('request') or row.get('id')
        if key:by_id[key][row['stage']].append(row)
        if row['stage']=='sync.target':targets[f['id']]=row
    db=sqlite3.connect((root/'state/state.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON');pubs=[]
    for at,raw,descriptor in db.execute("SELECT p.published_at_ms,i.record,p.descriptor FROM publications p JOIN incremental_runs i ON i.id=p.export_id WHERE p.state='published' ORDER BY p.ordinal"):
        record=json.loads(raw);m=json.loads(descriptor)['manifest']
        pubs.append((at,record,m))
    db.close();batches=[];previous_at=None
    for published,record,manifest in pubs:
        key=record['id'];stages=by_id[key]
        def one(stage):
            values=stages[stage];assert len(values)==1,(key,stage,len(values));return values[0]
        requested=one('apply.requested');dispatch=one('apply.dispatch');start=one('worker.start');end=one('worker.end');done=one('worker.done') if stages['worker.done'] else None;one('apply.published')
        req=requested['fields'];target=targets[req['parent']]
        after=rows_to(req['after_lsn']);target_rows=rows_to(req['target_lsn']);count=sum(t['rows'] for t in manifest['tables']);added=count-after
        row=dict(id=key,published_ms=published,published_rows=count,rows=added,after_rows=after,target_rows=target_rows,
            target_batch_rows=target_rows-after,available_durable_rows_at_target=durable_at(target['at_ms'])-after,
            available_durable_rows_at_admission=durable_at(requested['at_ms'])-after,
            committed_rows_at_admission=committed_at(requested['at_ms'])-after,
            target_excluded_durable_rows_at_admission=durable_at(requested['at_ms'])-target_rows,
            undurable_rows_at_admission=committed_at(requested['at_ms'])-durable_at(requested['at_ms']),
            report_age_at_target_ms=target['at_ms']-target['fields']['capture_observed_at_ms'],
            report_age_at_admission_ms=requested['at_ms']-req['capture_observed_at_ms'],
            target_to_admission_ms=requested['at_ms']-target['at_ms'],admission_to_dispatch_ms=dispatch['at_ms']-requested['at_ms'],
            dispatch_to_worker_ms=start['at_ms']-dispatch['at_ms'],worker_ms=end['fields']['wall_ns']/1e6,
            worker_cpu_ms=end['fields']['cpu_ns']/1e6,worker_to_done_ms=None if done is None else done['at_ms']-end['at_ms'],
            worker_to_publish_ms=published-end['at_ms'],done_event_observed=done is not None,
            done_to_publish_ms=None if done is None else published-done['at_ms'],after_manifest_ms=published-manifest['observed_at_ms'],
            prior_publication_to_admission_ms=None if previous_at is None else requested['at_ms']-previous_at,
            reuse=dispatch['fields']['reuse'],request_index=start['fields']['request_index'],pid=start['pid'],
            maxrss_bytes=end['fields']['maxrss_bytes'])
        if added:
            selected=one('journal.selected')['fields'];decoded=one('worker.decoded')['fields']
            assert decoded['rows']==added,(key,decoded['rows'],added)
            assert rows_to(decoded['end'])==count
            assert rows_to(selected['selected_end'])-after==sum(t['rows'] for t in transactions if lsn(req['after_lsn'])<t['end']<=selected['selected_end'])
            row.update(durable_rows_at_read=rows_to(selected['durable_lsn'])-after,
                selected_rows=rows_to(selected['selected_end'])-after,selected_bytes=selected['selected_bytes'],
                decoded_bytes=decoded['input_bytes'],decoded_transactions=decoded['transactions'],
                target_to_read_ms=selected['started_ms']-target['at_ms'],
                unused_durable_rows_at_read=rows_to(selected['durable_lsn'])-count,
                target_excluded_durable_rows_at_read=rows_to(selected['durable_lsn'])-target_rows,
                journal_cut_reached_target=lsn(selected['selected_end'])==lsn(req['target_lsn']),
                decode_consumed_journal=decoded['end']==selected['selected_end'])
        timings={stage:sum(e['fields']['wall_ns'] for e in records if 'wall_ns' in e['fields'])/1e6
            for stage,records in stages.items() if stage.startswith('worker.') and any('wall_ns' in e['fields'] for e in records)}
        row['nested_stage_ms']=timings;batches.append(row);previous_at=published
    first=next(b for b in batches if b['published_rows']>=13000000)
    last=next(b for b in batches if b['published_rows']>=14600000)
    late=[b for b in batches if first['published_ms']<b['published_ms']<=last['published_ms']]
    def summarize(selected):
        numeric=[k for k,v in next(b for b in selected if b['done_event_observed']).items() if isinstance(v,(int,float)) and not isinstance(v,bool) and k not in ('pid','published_ms')]
        return dict(count=len(selected),statistics={k:stats([b[k] for b in selected if b.get(k) is not None]) for k in numeric},
            stages_ms={k:stats([b['nested_stage_ms'].get(k,0) for b in selected]) for k in sorted({k for b in selected for k in b['nested_stage_ms']})},
            missing_done_events=sum(not b['done_event_observed'] for b in selected),fresh_processes=sum(b['request_index']==1 for b in selected),sampled_processes=len({b['pid'] for b in selected}),
            journal_target_cuts=sum(b.get('journal_cut_reached_target',False) for b in selected),decode_full_journal=sum(b.get('decode_consumed_journal',False) for b in selected),
            cold_verify_ms=stats([b['nested_stage_ms'].get('worker.verify_previous',0) for b in selected if b['request_index']==1]),
            warm_verify_ms=stats([b['nested_stage_ms'].get('worker.verify_previous',0) for b in selected if b['request_index']>1]))
    late_transactions=[t for t in positive if first['published_rows']<rows_to(t['end'])<=last['published_rows']]
    def source_summary(selected):return dict(transactions=len(selected),copy_commit_ms=stats([t['copy_and_commit_ms'] for t in selected]),commit_to_durable_ms=stats([t['durable_ms']-t['committed_at_ms'] for t in selected]))
    return dict(status='PROFILE_JOIN_PASS',scope=__doc__,release_identity=result['release_identity'],rows=result['load_rows'],
        caveats=['Rust event clock has 1 ms resolution; availability at event boundaries can differ within that millisecond.',
                 'Nested worker spans and observer counters must not be added as independent wall-time stages.',
                 'Source copy ledger matches positive-row transactions by order, exact rows and pre-commit LSN interval; LSN bytes never estimate row counts.',
                 'No hook added inside COPY; source copy+commit timings come from frozen harness.',
                 'A worker can be retired after its done marker is consumed but before the diagnostic done event. Missing events retain worker-end to publication bounds; they do not invent a handoff timestamp.',
                 'Profiled performance is diagnostic, not an optimization qualification.'],
        health=dict(processes=health,daemon={k:daemon[-1][k] for k in ['profile_write_ns','profile_write_errors','event_bytes','event_dropped','budget_exceeded']}),
        source=dict(all=source_summary(positive),late=source_summary(late_transactions),
            flow_control_wait_seconds=result['flow_control_wait_seconds'],load_seconds=result['load_seconds'],
            summed_copy_and_commit_seconds=sum(a['copy_and_commit_ms'] for a in acks)/1000),
        all=summarize(batches),late=summarize(late),late_window=dict(first=first['published_rows'],last=last['published_rows']),batches=batches)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--load',type=Path,required=True);parser.add_argument('--profile',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=analyze(args.load,args.profile)
    with args.output.open('x') as stream:json.dump(result,stream,indent=2);stream.write('\n')
