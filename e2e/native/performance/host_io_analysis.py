#!/usr/bin/env python3
"""Review observer controls; process I/O counters are inclusive, not additive physical I/O."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import statistics

from host_io import deltas
from compare import expected
from comparison import load_trial


def read(p):return json.loads(gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes())


def summarize(data,start,end):
    group=data['owned_container_cgroup_sha256'];assert group and not data['error']
    rows=[s for s in data['samples'] if start<=s['at_ms']<=end];assert len(rows)>=2
    totals={}
    gaps=dict(new_identities=0,exited_identities=0,unavailable_intervals=0)
    for a,b in zip(rows,rows[1:]):
        assert b['at_ms']>a['at_ms'] and b['at_ms']-a['at_ms']<10000
        d=deltas(a,b)
        for k in ('new_identities','exited_identities'):gaps[k]+=d[k]
        gaps['unavailable_intervals']+=len(d['unavailable'])
        for r in d['processes']:
            identity=(r['pid'],r['start_ticks'],r['cgroup_sha256'])
            value=totals.setdefault(identity,dict(pid=r['pid'],start_ticks=r['start_ticks'],
                role=r['role'],cgroup_sha256=r['cgroup_sha256'],owned=r['cgroup_sha256']==group,
                read_bytes=0,write_bytes=0,cpu_ticks=0))
            for key in ('read_bytes','write_bytes','cpu_ticks'):value[key]+=r[key]
    return dict(covered_seconds=(rows[-1]['at_ms']-rows[0]['at_ms'])/1000,process_counter_deltas=[totals[k] for k in sorted(totals)],
        coverage_gaps=gaps,permission_denied_samples=sum(s['permission_denied'] for s in rows),
        scope='Separate stable process counters. /proc/pid/io includes waited-for children: never sum parent/child values or interpret them as disjoint physical I/O or a lower bound. Turnover/inaccessible counters are missing coverage, not idle. Causality is not established.')


def analyze(root):
    record=read(root/'experiment.json');assert record['state']=='complete'
    assert record['config']['host_io']=='candidate'
    rows=[];pairs=[]
    for attempt in record['attempts']:
        outcomes={}
        for arm,receipt in attempt['results'].items():
            folder=root/receipt['directory']
            for name,digest in receipt['evidence_sha256'].items():
                path=(folder/name).resolve();assert path.is_relative_to(root.resolve())
                assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
            assert load_trial(folder,expected(record['config'],arm,attempt['pair']))==receipt['metrics']
            t=read(next(folder.glob('*-cpu*/trial.json')));c=read(folder/'observer-controller.json')
            row=dict(directory=receipt['directory'],arm=arm,accepted=attempt['accepted'],metrics=receipt['metrics'],
                controller_cpu_cores=c['cpu_seconds']/c['elapsed_seconds'],controller_sampled_peak_rss_bytes=c['sampled_peak_rss_bytes'],controller_scope=c['scope'])
            def io_values(text):
                return {parts[0]:{k:int(v) for k,v in (x.split('=') for x in parts[1:])}
                        for parts in (line.split() for line in text.splitlines())}
            before=io_values(t['cgroup_before']['io.stat']);after=io_values(t['cgroup_after']['io.stat'])
            row['fixture_cgroup_io_delta']={dev:{k:value-before.get(dev,{}).get(k,0) for k,value in counters.items()} for dev,counters in after.items()}
            assert all(v>=0 for counters in row['fixture_cgroup_io_delta'].values() for v in counters.values())
            row['fixture_io_scope']='Cgroup load bookends include launch delay and final transaction tail; distinct from sampled process counters.'
            if arm=='candidate':
                d=read(folder/'host-io.json.gz');row['host_io']=summarize(d,t['measurement_start_ms'],t['measurement_end_ms'])
                row['sampler_wall_ns']=d['monitor_wall_ns'];row['sampler_raw_sample_bytes']=d['sample_bytes']
            rows.append(row);outcomes[arm]=row
        if attempt['accepted']:
            assert len(outcomes)==2
            a,b=outcomes['predecessor'],outcomes['candidate']
            pairs.append(dict(repeat=attempt['pair']['repeat'],cpus=attempt['pair']['cpus'],
                fixture_percent={k:100*(b['metrics'][k]/a['metrics'][k]-1) for k in ('source_rows_s','lag_p95_ms','cpu_cores','peak_memory_bytes')},
                controller_additional_cores=b['controller_cpu_cores']-a['controller_cpu_cores'],
                controller_additional_sampled_peak_rss_bytes=b['controller_sampled_peak_rss_bytes']-a['controller_sampled_peak_rss_bytes']))
    return dict(status='measurements_complete_review_required',runtime_change=False,
        scope='Observer control only. Original SP06 outcomes retained; no source fix or historical cause established.',
        accepted_pairs=len(pairs),attempts=len(record['attempts']),pairs=pairs,trials=rows,
        paired_fixture_percent_medians={k:statistics.median(p['fixture_percent'][k] for p in pairs) for k in pairs[0]['fixture_percent']},
        controller_additional_cores_median=statistics.median(p['controller_additional_cores'] for p in pairs),
        controller_additional_sampled_peak_rss_bytes_median=statistics.median(p['controller_additional_sampled_peak_rss_bytes'] for p in pairs))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('campaign',type=Path);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.write_text(json.dumps(analyze(a.campaign),indent=2)+'\n')
