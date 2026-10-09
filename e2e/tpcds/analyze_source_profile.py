#!/usr/bin/env python3
"""Reconcile EQ232 COPY phases against every frozen-source attempt and ack."""
import argparse
from collections import Counter
import datetime
import gzip
import hashlib
import json
from pathlib import Path
import statistics

PHASES = ('begin', 'copy_start', 'copy_write', 'copy_finish', 'lsn_query', 'commit')
LOW, HIGH = 13_000_000, 14_600_000


def rows(path):
    path=Path(path)
    stream=path.open() if path.exists() else gzip.open(str(path)+'.gz','rt')
    with stream:
        for line in stream:
            yield json.loads(line)


def distribution(values):
    values = sorted(values)
    if not values:
        return None
    return dict(n=len(values), mean=statistics.mean(values), p50=statistics.median(values),
                p95=values[min(len(values)-1, int(.95*len(values)))], maximum=values[-1], total=sum(values))


def reconcile(transactions, ledger):
    assert len(ledger) == 2 * len(transactions), 'missing source attempt or acknowledgment'
    committed = 0
    for index, event in enumerate(transactions):
        attempt, ack = ledger[2*index:2*index+2]
        assert attempt['kind'] == 'attempt' and ack['kind'] == 'ack'
        assert event['success'] and event['ordinal'] == index+1
        assert event['rows'] == attempt['rows'] == ack['rows']
        assert event['encoded_bytes'] == attempt['encoded_bytes']
        assert attempt['table'] == ack['table'] and attempt['end_offset'] == ack['end_offset']
        committed += event['rows']; assert event['committed_rows'] == committed
        assert all(event.get(k+'_ns', -1) >= 0 for k in PHASES)
        assert event['end_ns'] - event['start_ns'] >= sum(event[k+'_ns'] for k in PHASES)
        event.update(table=ack['table'], ack_ms=ack['copy_and_commit_ms'], copy_sha256=attempt['copy_sha256'])
    return committed


def phase_summary(events):
    elapsed = (events[-1]['end_ns'] - events[0]['start_ns']) / 1e9
    total = sum(e['rows'] for e in events)
    phase = {k: distribution([e[k+'_ns']/1e6 for e in events]) for k in PHASES}
    return dict(transactions=len(events), rows=total, first_committed_rows=events[0]['committed_rows'],
        last_committed_rows=events[-1]['committed_rows'], elapsed_seconds=elapsed, rows_s=total/elapsed,
        active_transaction_rows_s=total/(sum(e['end_ns']-e['start_ns'] for e in events)/1e9),
        phases_ms=phase, frozen_copy_and_commit_ms=distribution([e['ack_ms'] for e in events]),
        unassigned_inside_transaction_ms=distribution([(e['end_ns']-e['start_ns']-sum(e[k+'_ns'] for k in PHASES))/1e6 for e in events]),
        non_transaction_gap_seconds=elapsed-sum(e['end_ns']-e['start_ns'] for e in events)/1e9)


def sample_summary(samples, events):
    first, last = events[0]['start_ns'], events[-1]['end_ns']
    selected = [s for s in samples if first <= s['at_ns'] <= last]
    waits = Counter((s['phase'], *(w or ['missing',None,None])) for s in selected for w in (s['waits'] or [[]]))
    result = dict(samples=len(selected), waits=[dict(phase=key[0],state=key[1],wait_type=key[2],wait=key[3],samples=value) for key,value in waits.most_common()])
    resources = [s for s in selected if 'storage' in s]
    assert len(resources) >= 2, 'missing storage observations'
    a,b=resources[0],resources[-1]
    result['counter_window'] = dict(start_rows=a['committed_rows'], end_rows=b['committed_rows'], seconds=(b['at_ns']-a['at_ns'])/1e9,
        note='First/last two-second samples strictly within transaction cohort; counter deltas have this narrower scope.')
    def subtract(before, after):
        assert before.keys() == after.keys(), 'counter identity changed'
        delta = {k:after[k]-before[k] for k in before}
        assert all(v >= 0 for v in delta.values()), 'counter reset or invalid sample'
        return delta
    result['database_delta'] = subtract(dict(zip(('xact_commit','blks_read','blks_hit','read_ms','write_ms','temp_bytes'),a['database'])),dict(zip(('xact_commit','blks_read','blks_hit','read_ms','write_ms','temp_bytes'),b['database'])))
    def tables(row):
        return {name+':'+k:v for name,*values in row['tables'] for k,v in zip(('heap_read','heap_hit','index_read','index_hit'),values)}
    result['table_delta'] = subtract(tables(a),tables(b))
    for name in ('neon_backend',):
        if name in a and name in b:
            result[name+'_delta'] = {k: v for k,v in subtract(
                {m:v for m,_,v in a[name] if m.endswith(('_total','_sum','_count'))},
                {m:v for m,_,v in b[name] if m.endswith(('_total','_sum','_count'))}).items() if v}
    result['storage_counter_deltas'] = {}
    for role in a['storage']:
        # Some tenant/layer counters appear only after startup: retain common keys
        # and list omissions instead of interpreting gauges as cumulative time.
        aa,bb=a['storage'][role],b['storage'][role]
        names = set(aa)&set(bb)
        names = {n for n in names if n.split('|')[0].endswith(('_total','_sum','_count'))}
        result['storage_counter_deltas'][role] = {n:bb[n]-aa[n] for n in sorted(names) if bb[n]!=aa[n]}
        result.setdefault('storage_added_metrics',{})[role] = sorted(set(bb)-set(aa))
        result.setdefault('storage_removed_metrics',{})[role] = sorted(set(aa)-set(bb))
    return result


def resource_summary(samples, events):
    def stamp(row):
        return datetime.datetime.fromisoformat(row['utc']).timestamp()*1e9
    selected=[s for s in samples if events[0]['start_unix_ns'] <= stamp(s) <= events[-1]['end_unix_ns']]
    assert len(selected)>=2, 'missing cgroup samples'
    a,b=selected[0],selected[-1]; elapsed=(stamp(b)-stamp(a))/1e9
    def counters(text):
        return {k:int(v) for k,v in (line.split() for line in text.splitlines())}
    def delta(name):
        aa,bb=counters(a[name]),counters(b[name])
        return {k:bb[k]-aa[k] for k in aa.keys()&bb.keys()}
    cpu=delta('cpu.stat'); memory=delta('memory.stat')
    pressure={}
    for name in ('memory.pressure','io.pressure','cpu.pressure'):
        def totals(s):
            return {line.split()[0]:int(line.split('total=')[1]) for line in s.splitlines()}
        aa,bb=totals(a[name]),totals(b[name])
        pressure[name]={k:(bb[k]-aa[k])/1e6 for k in aa}
    roles={}; initial={}; final={}
    for s in selected:
        for p in s['processes']:
            key=(p['pid'],p['start_ticks']); value=p['user_ticks']+p['system_ticks']
            if s is a:initial[key]=value
            final[key]=(p['role'],value)
    for key,(role,value) in final.items():
        roles[role]=roles.get(role,0)+(value-initial.get(key,0))/a['clock_ticks_per_second']/elapsed
    return dict(samples=len(selected),seconds=elapsed,cgroup_cpu_cores=cpu['usage_usec']/1e6/elapsed,
        sampled_process_cpu_cores=roles,
        process_note='Retired processes use their last observed CPU counter; short-lived work can be missed. Cgroup CPU is authoritative for the cell total.',
        memory_current_bytes=distribution([int(s['memory.current']) for s in selected]),
        memory_first=counters(a['memory.stat']),memory_last=counters(b['memory.stat']),
        memory_event_delta=delta('memory.events'),
        reclaim_fault_delta={k:v for k,v in memory.items() if k.startswith(('pgscan','pgsteal','pgfault','pgmajfault','workingset_'))},
        pressure_stall_seconds=pressure,io_first=a['io.stat'],io_last=b['io.stat'],
        host_disks_first=a['host_diskstats'],host_disks_last=b['host_diskstats'],
        observed_compilers=[p for s in selected for p in s['observed_compilers']])


def analyze(root, qualification=None):
    root=Path(root); report=json.loads((root/'result.json').read_text())
    assert report['stopped']
    if qualification is None:
        assert report['status']=='PREFIX_PASS'
    else:
        qualified=json.loads(Path(qualification).read_text())
        assert report['status']=='FAIL' and report['stage']=='post_timing_sync_bootstrap'
        assert qualified['status']=='PREFIX_PASS' and qualified['stopped']
        assert qualified['source_load_receipt_sha256']==hashlib.sha256((root/'result.json').read_bytes()).hexdigest()
        assert qualified['release_identity']==report['release_identity']
        assert qualified['committed_rows']==report['committed_rows']
    observer=json.loads((root/'source-profile/observer.json').read_text())
    assert not observer['errors'] and observer['samples']>0
    for kind in ('transactions','storage'):
        assert not observer[kind]['errors'] and not observer[kind]['dropped'], 'incomplete observation ledger'
    assert all(observer['capabilities'].values()), 'missing Neon attribution views'
    events=list(rows(root/'source-profile/transactions.jsonl'))
    assert reconcile(events,list(rows(root/'commits.jsonl'))) == report['committed_rows'] == 14770127
    samples=list(rows(root/'source-profile/storage.jsonl'))
    late=[e for e in events if LOW <= e['committed_rows'] <= HIGH]
    resources=list(rows(root.parent/(root.name+'-control')/'resources.jsonl'))
    return dict(scope='Diagnostic source attribution, not optimization qualification. Late cohort uses source acknowledgments, not the EQ230 publication cohort.',
        rows=report['committed_rows'],release_identity=report['release_identity'],observer=observer,
        original_load_status=report['status'],post_timing_qualification=qualification is not None,
        overall=phase_summary(events),late=phase_summary(late),late_observations=sample_summary(samples,late),
        late_resources=resource_summary(resources,late),
        load_seconds=report['load_seconds'],flow_control_wait_seconds=report['flow_control_wait_seconds'])


def compare(a,b):
    aa=list(rows(Path(a)/'commits.jsonl'));bb=list(rows(Path(b)/'commits.jsonl'))
    aa=[r for r in aa if r['kind']=='attempt'];bb=[r for r in bb if r['kind']=='attempt']
    assert aa == bb, 'arms differ in COPY bytes, order, transaction shape or offsets'
    return dict(status='EXACT_COPY_INPUTS_MATCH',transactions=len(aa),rows=sum(r['rows'] for r in aa))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('--compare',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--qualification',type=Path)
    args=p.parse_args();result=analyze(args.root,args.qualification)
    if args.compare:result['paired_inputs']=compare(args.root,args.compare)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
