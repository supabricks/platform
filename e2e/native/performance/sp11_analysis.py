"""Predeclared SP11 steady-load screens; no extrapolation to unmeasured gates."""
import bisect
import gzip
import math
import statistics
import struct

EVENT = struct.Struct('!dd')  # source COMMIT acknowledgement, observed publication (milliseconds)
POLICY = dict(window_seconds=300, stride_seconds=60, minimum_rows_s=1000,
              version=2, memory_metric='cgroup_current_minus_clean_inactive_file',
              maximum_p95_ms=5000, maximum_drain_seconds=120,
              backlog_growth_ratio=1.25, backlog_growth_allowance_bytes=1024**2,
              memory_growth_ratio=1.25, memory_growth_allowance_bytes=256*1024**2,
              spool_physical_limit_bytes=512*1024**2, maximum_sample_gap_ms=5000)


def percentile(values, p):
    if not values:
        raise ValueError('empty latency window')
    values = sorted(values)
    return values[math.ceil(len(values)*p/100)-1]


def read_events(path):
    with gzip.open(path, 'rb') as stream:
        data = stream.read(128*1024**2+1)
    if len(data)>128*1024**2 or len(data)%EVENT.size:
        raise ValueError('invalid event stream size')
    rows = list(EVENT.iter_unpack(data))
    if not rows or any(not math.isfinite(a) or not math.isfinite(p) for a,p in rows):
        raise ValueError('invalid event timestamp')
    # Publication can precede client receipt of COMMIT; use the existing zero floor.
    return rows


def working_memory(row):
    # Keep active file memory, kernel memory and all dirty/writeback bytes in
    # the screen. Only discount clean inactive file cache. No process RSS is
    # subtracted, including the qualifier's own memory. Readings are not atomic;
    # charging dirty/writeback conservatively can overcount but cannot hide it.
    stat=row['cgroup_memory_stat'];total=row['cgroup_memory_bytes']
    fields=[stat[k] for k in ('inactive_file','file_dirty','file_writeback')]
    if total<0 or any(v<0 for v in fields):raise ValueError('invalid memory counters')
    clean=max(0,fields[0]-fields[1]-fields[2])
    if clean>total:raise ValueError('inconsistent cgroup memory counters')
    return total-clean


def memory_attribution(resources,start,end,width):
    """Raw diagnostic breakdown and conservative working-memory medians."""
    result={}
    for name,rows in [('early',[r for r in resources if start<=r['at_ms']<start+width]),
                      ('late',[r for r in resources if end-width<=r['at_ms']<=end])]:
        if not rows:raise ValueError('missing memory attribution')
        values=dict(qualifier_rss_bytes=[r['qualifier_rss_bytes'] for r in rows],
                    cgroup_memory_bytes=[r['cgroup_memory_bytes'] for r in rows],
                    working_memory_bytes=[working_memory(r) for r in rows])
        for field in ('anon','file','kernel','inactive_file','file_dirty','file_writeback'):
            values[field+'_bytes']=[r['cgroup_memory_stat'][field] for r in rows]
        result[name]={k:statistics.median(v) for k,v in values.items()}
    return result


def analyze(report, events, resources, *, screen=False):
    if report['status']!='measured':
        raise ValueError('only complete measured trials have a window qualification')
    start,end=report['measurement_start_ms'],report['measurement_end_ms']
    if not start<end or len(events)!=report['source']['completed_transactions']:
        raise ValueError('transaction count/window differs')
    events=sorted(events);acks=[a for a,p in events];publications=sorted(p for a,p in events)
    if acks[0]<start or acks[-1]>end:
        raise ValueError('source acknowledgement outside measurement')
    width=min(POLICY['window_seconds']*1000,end-start) if screen else POLICY['window_seconds']*1000
    if end-start<width:raise ValueError('short production window')
    starts=[];point=start
    while point+width<=end:
        starts.append(point);point+=POLICY['stride_seconds']*1000
    if not starts or end-width>starts[-1]+1:starts.append(end-width)
    windows=[]
    for left in starts:
        right=left+width;i=bisect.bisect_left(acks,left);j=bisect.bisect_left(acks,right)
        lags=[max(0,p-a) for a,p in events[i:j]]
        published=bisect.bisect_left(publications,right)-bisect.bisect_left(publications,left)
        windows.append(dict(start_ms=left,end_ms=right,transactions=j-i,
            committed_rows_s=2*(j-i)/(width/1000),published_rows_s=2*published/(width/1000),
            p95_ms=percentile(lags,95),p99_ms=percentile(lags,99),maximum_ms=max(lags)))
    series=[s for s in report['backlog_series'] if start<=s['at_ms']<=end]
    early=[s for s in series if s['at_ms']<start+width]
    late=[s for s in series if s['at_ms']>=end-width]
    if not early or not late or any(s.get('backlog_bytes') is None for s in series):
        raise ValueError('missing backlog observation')
    early_backlog=percentile([s['backlog_bytes'] for s in early],95)
    late_backlog=percentile([s['backlog_bytes'] for s in late],95)
    early_memory=statistics.median(s['memory_bytes'] for s in early)
    late_memory=statistics.median(s['memory_bytes'] for s in late)
    xs=[(s['at_ms']-start)/1000 for s in series];ys=[s['backlog_bytes'] for s in series]
    xm=statistics.mean(xs);ym=statistics.mean(ys);denominator=sum((x-xm)**2 for x in xs)
    slope=sum((x-xm)*(y-ym) for x,y in zip(xs,ys))/denominator if denominator else 0
    sampled=[s for s in resources if start<=s['at_ms']<=end]
    if not sampled:raise ValueError('missing resource samples')
    attribution=memory_attribution(sampled,start,end,width)
    early_working=attribution['early']['working_memory_bytes'];late_working=attribution['late']['working_memory_bytes']
    def pressure(counters):
        events={k:int(v) for k,v in (line.split() for line in counters['memory.events'].splitlines())}
        return {k:events[k] for k in ('high','max','oom','oom_kill')}
    before,after=pressure(report['cgroup_before']),pressure(report['cgroup_after'])
    pressure_delta={k:after[k]-before[k] for k in before}
    if any(v<0 for v in pressure_delta.values()):raise ValueError('memory events regressed')
    stamps=[start]+[s['at_ms'] for s in sampled]+[end]
    if any(b<a for a,b in zip(stamps,stamps[1:])):raise ValueError('resource clock regressed')
    gap=max(b-a for a,b in zip(stamps,stamps[1:]))
    denials=sum('error' in p for s in sampled for p in s['processes'])
    bounds=dict(early_backlog_p95_bytes=early_backlog,late_backlog_p95_bytes=late_backlog,
        backlog_slope_bytes_s=slope,early_memory_median_bytes=early_memory,late_memory_median_bytes=late_memory,
        early_working_memory_median_bytes=early_working,late_working_memory_median_bytes=late_working,
        memory_pressure_events=pressure_delta,
        peak_spool_bytes=max(s['spool_bytes'] for s in sampled),inspection_denials=denials,maximum_sample_gap_ms=gap)
    gates=dict(all_committed_windows=all(w['committed_rows_s']>=POLICY['minimum_rows_s'] for w in windows),
        all_published_windows=all(w['published_rows_s']>=POLICY['minimum_rows_s'] for w in windows),
        all_fresh_windows=all(w['p95_ms']<=POLICY['maximum_p95_ms'] for w in windows),
        bounded_drain=report['drain_seconds']<=POLICY['maximum_drain_seconds'],
        backlog_growth=late_backlog<=early_backlog*POLICY['backlog_growth_ratio']+POLICY['backlog_growth_allowance_bytes'],
        backlog_slope=slope<=POLICY['backlog_growth_allowance_bytes']/((end-start)/1000),
        memory_growth=late_working<=early_working*POLICY['memory_growth_ratio']+POLICY['memory_growth_allowance_bytes'],
        no_memory_pressure=not any(pressure_delta.values()),
        spool_budget=bounds['peak_spool_bytes']<=POLICY['spool_physical_limit_bytes'],inspectable=denials==0,continuous_resource_evidence=gap<=POLICY['maximum_sample_gap_ms'])
    return dict(status='screen_only' if screen else 'passed' if all(gates.values()) else 'failed',
        policy=POLICY,windows=windows,worst_window_p95_ms=max(w['p95_ms'] for w in windows),
        bounds=bounds,gates=gates,memory_attribution=attribution,
        raw_cgroup_memory_growth_screen=late_memory<=early_memory*POLICY['memory_growth_ratio']+POLICY['memory_growth_allowance_bytes'],
        limits=['Publication rates count only measured source transactions published within each wall-clock window.',
                'Lag follows COMMIT cohorts, including subsequent bounded drain; cadence is 60 seconds, not every possible rolling window.',
                'Memory policy v2 discounts only clean inactive file cache; raw cgroup growth remains a diagnostic screen. Qualifier memory, dirty/writeback file bytes and kernel memory remain charged.',
                'Sampled spool size is not whole-stack storage or a hard quota; file-cache growth does not establish bounded disk retention.',
                'Passing steady screens does not establish maintenance cycles, pinned-reader, pressure, scaling or recovery qualification.'])
