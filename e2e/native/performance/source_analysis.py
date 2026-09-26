#!/usr/bin/env python3
"""Recompute source capacity, controls and sampled wait/storage attribution."""
from collections import Counter,defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import sys


def read(path):return json.loads(gzip.decompress(path.read_bytes()) if path.suffix=='.gz' else path.read_bytes())
def describe(values):
    return dict(n=len(values),median=statistics.median(values),minimum=min(values),maximum=max(values)) if values else dict(n=0,median=None,minimum=None,maximum=None)
def percent_change(before,after):return 100*(after-before)/before if before else None


def select_client(trials,cpus=(8,16),repeats=3):
    # Prefer headroom across every full minute/repeat at both CPU sizes.
    for target in (1250,1000):
        for client in sorted({t['clients'] for t in trials}):
            selected=[t for t in trials if t['clients']==client and t['cpus'] in cpus]
            if all(len([t for t in selected if t['cpus']==c and t['profile']])==repeats
                   and {t['repeat'] for t in selected if t['cpus']==c and t['profile']}==set(range(1,repeats+1))
                   for c in cpus):
                if all(t['seconds']==300 and len(t['minute_rows_s'])==5 and min(t['minute_rows_s'])>=target for t in selected):
                    return dict(clients=client,qualification_floor_rows_s=target,preferred_headroom=target==1250)
    return None


def io_values(text):
    result={}
    for line in text.splitlines():
        device,*values=line.split();result[device]={k:int(v) for k,v in (x.split('=') for x in values)}
    return result


def profile_summary(profile,source):
    start,end=source['measurement_start_ms'],source['measurement_end_ms']
    rows=[r for r in profile['observations'] if start<=r['at_ms']<end]
    assert rows and not profile['monitor_errors']
    active=Counter();all_states=Counter()
    for row in rows:
        for wait in row['pg_waits']:
            if wait['backend_type']!='client backend':continue
            all_states[wait['state'] or 'unknown']+=wait['count']
            if wait['state']=='active':active[(wait['wait_type'] or 'running')+'/'+(wait['wait'] or 'running')]+=wait['count']
    total=sum(active.values())
    storage=[r for r in rows if 'storage_metrics' in r]
    assert len(storage)>=2
    first,last=storage[0],storage[-1];seconds=(last['at_ms']-first['at_ms'])/1000
    a=first['storage_metrics']['safekeeper'];b=last['storage_metrics']['safekeeper']
    def diff(key):
        value=b[key]-a[key];assert value>=-1e-9,key;return max(0,value)
    count=diff('safekeeper_flush_wal_seconds_count');duration=diff('safekeeper_flush_wal_seconds_sum')
    histogram={key.split('|le=')[1]:diff(key) for key in a if key.startswith('safekeeper_flush_wal_seconds_bucket|le=')}
    flush_p95=next((bound for bound,value in sorted(histogram.items(),key=lambda x:float(x[0])) if value>=.95*count),None) if count else None
    wal={k:last['pg_wal'][k]-first['pg_wal'][k] for k in first['pg_wal']}
    assert all(v>=0 for v in wal.values())
    resources=[r for r in profile['resources'] if start<=r['at_ms']<end];assert len(resources)>=2
    ra,rb=resources[0],resources[-1];resource_seconds=(rb['at_ms']-ra['at_ms'])/1000
    disks={}
    for device,old in ra['host_disks'].items():
        new=rb['host_disks'].get(device)
        if new is None:continue
        d={k:new[k]-v for k,v in old.items()};assert min(d.values())>=0
        if any(d.values()):disks[device]=dict(delta=d,busy_percent=100*d['io_ms']/(resource_seconds*1000),weighted_queue_mean=d['weighted_io_ms']/(resource_seconds*1000))
    cg_a=io_values(ra['cgroup']['io.stat']);cg_b=io_values(rb['cgroup']['io.stat'])
    cgroup={dev:{k:v-cg_a.get(dev,{}).get(k,0) for k,v in counters.items()} for dev,counters in cg_b.items()}
    assert all(v>=0 for counters in cgroup.values() for v in counters.values())
    return dict(observations=len(rows),active_client_samples=total,
        active_client_waits={k:dict(samples=v,share_percent=100*v/total if total else None) for k,v in sorted(active.items())},
        client_state_samples=dict(all_states),storage_covered_seconds=seconds,
        safekeeper_flush=dict(calls=count,total_ms=duration*1000,mean_ms=duration*1000/count if count else None,
            calls_s=count/seconds,p95_bucket_upper_seconds=flush_p95,buckets=histogram),
        safekeeper_written_bytes=diff('safekeeper_written_wal_bytes_total'),pg_wal_delta=wal,
        pg_wal_time_available=profile['pg_settings']['track_wal_io_timing']=='on',
        resource_covered_seconds=resource_seconds,load_peak_memory_bytes=max(int(r['cgroup']['memory.current']) for r in resources),
        cgroup_io_delta=cgroup,host_disk_deltas=disks,monitor_wall_ns=profile['monitor_wall_ns'],process_sample_errors=profile['process_sample_errors'])


def analyze(root):
    experiment=read(root/'experiment.json');assert experiment['state']=='complete'
    assert [b['block'] for b in experiment['blocks']]==experiment['config']['order'],'incomplete or changed block order'
    assert [a for a in experiment['attempts'] if a['accepted']]==experiment['blocks'],'accepted attempt history differs'
    trials=[]
    for block in experiment['blocks']:
        assert block['accepted'] and len(block['results'])==len(block['block']['variants'])
        for result in block['results']:
            folder=root/result['directory']
            for name,checksum in result['evidence_sha256'].items():assert hashlib.sha256((folder/name).read_bytes()).hexdigest()==checksum,name
            report=read(folder/'trial.json');source=report['source'];assert report['status']=='measured'
            cleanup=read(folder/'cleanup.json')
            assert cleanup['exit_code']==0 and cleanup['remaining_descendants']==cleanup['leaked_descendants']==0 and not cleanup['timed_out']
            samples=read(folder/'source-samples.json.gz')['samples']
            # Reconstruct exact in-window and per-minute denominators from raw timings.
            seconds=source['measurement_seconds'];inside=[s for s in samples if 0<=s[0]<seconds]
            minute=[2*sum(begin<=s[0]<min(begin+60,seconds) for s in inside)/(min(begin+60,seconds)-begin) for begin in range(0,seconds,60)]
            assert len(samples)==source['committed_transactions'] and len(inside)==source['in_window_transactions']
            assert source['committed_changed_rows_s']==2*len(inside)/seconds
            assert minute==[w['changed_rows_s'] for w in source['windows']]
            t=dict(directory=result['directory'],cpus=block['block']['cpus'],repeat=block['block']['repeat'],
                clients=report['parameters']['clients'],profile=report['parameters']['profile'],seconds=seconds,
                rows_s=source['committed_changed_rows_s'],minute_rows_s=minute,min_minute_rows_s=min(minute),
                transaction_p95_ms=source['transaction_ms']['p95'],transaction_p99_ms=source['transaction_ms']['p99'],
                commit_p95_ms=source['sql_ms'].get('commit_ms',{}).get('p95'),
                cpu_cores=report['cpu']['average_cpu_cores'],peak_memory_bytes=report['peak_memory_bytes'],
                tail_transactions=source['tail_transactions'],attribution=None)
            if t['profile']:t['attribution']=profile_summary(read(folder/'profile.json.gz'),source)
            trials.append(t)
    groups=[]
    for cpu,clients,enabled in sorted({(t['cpus'],t['clients'],t['profile']) for t in trials}):
        selected=[t for t in trials if (t['cpus'],t['clients'],t['profile'])==(cpu,clients,enabled)]
        groups.append(dict(cpus=cpu,clients=clients,profile=enabled,
            metrics={k:describe([t[k] for t in selected if t[k] is not None]) for k in ('rows_s','min_minute_rows_s','transaction_p95_ms','transaction_p99_ms','commit_p95_ms','cpu_cores','peak_memory_bytes','tail_transactions')},
            syncrep_active_share_percent=describe([t['attribution']['active_client_waits'].get('IPC/SyncRep',{}).get('share_percent',0) for t in selected]) if enabled else None,
            safekeeper_flush_mean_ms=describe([t['attribution']['safekeeper_flush']['mean_ms'] for t in selected]) if enabled else None))
    controls=[]
    if experiment['config']['mode']=='controls':
        for cpu in sorted({t['cpus'] for t in trials}):
            paired=defaultdict(list)
            for repeat in sorted({t['repeat'] for t in trials if t['cpus']==cpu}):
                a,b=(next(t for t in trials if t['cpus']==cpu and t['repeat']==repeat and t['profile']==p) for p in (False,True))
                for key in ('rows_s','min_minute_rows_s','transaction_p95_ms','cpu_cores','peak_memory_bytes'):
                    value=percent_change(a[key],b[key])
                    if value is not None:paired[key].append(value)
            controls.append(dict(cpus=cpu,paired_percent={k:describe(v) for k,v in paired.items()}))
    return dict(scope='Three-repeat source-only screen, not a replication-engine speedup or EC2 claim. Rates exclude tail acknowledgments. PG wait shares are sampled active-client observations, not exact time. Safekeeper histograms provide bucket bounds. Host disks are shared; devices may overlap and must not be summed. Memory peak includes setup/warmup. Profile-disabled attribution is unavailable, not zero.',
        accepted_blocks=len(experiment['blocks']),attempts=len(experiment['attempts']),
        retained_rejected_blocks=sum(not a['accepted'] for a in experiment['attempts']),
        provisional_selection=select_client(trials) if experiment['config']['mode']=='screen' else None,
        groups=groups,controls=controls,trials=trials)


if __name__=='__main__':print(json.dumps(analyze(Path(sys.argv[1])),indent=2,sort_keys=True))
