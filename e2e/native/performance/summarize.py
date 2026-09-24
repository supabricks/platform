#!/usr/bin/env python3
"""Summarize complete trial reports; never convert failures into latency samples."""
import argparse
import collections
import csv
import gzip
import json
from pathlib import Path
import statistics


def summarize(root):
    archive=json.loads(gzip.decompress((root/'raw-reports.json.gz').read_bytes())) if (root/'raw-reports.json.gz').exists() else None
    manifest=archive['matrix'] if archive else json.loads((root/'matrix.json').read_text())
    if archive and (root/'matrix.json').exists():assert manifest==json.loads((root/'matrix.json').read_text()),'archive manifest differs'
    if not manifest.get('completed_at'):raise ValueError('matrix is incomplete')
    if len(manifest['trials'])!=len(manifest['order']):raise ValueError('missing trials')
    contention=json.loads((root/'host-contention.json').read_text()) if (root/'host-contention.json').exists() else None
    records=[]
    for t,expected in zip(manifest['trials'],manifest['order']):
        assert all(t[k]==expected[k] for k in ('cpus','rate','repeat'))
        d=archive['trials'][t['name']]['trial'] if archive else json.loads((root/t['name']/'trial.json').read_text())
        assert d['status']==t['status'] and d['status'] in ('measured','runtime_failed')
        assert d['parameters']['rate']==t['rate']
        if d['status']=='measured':
            assert 'both_published_tables_equal_frozen_postgres_source' in d['checks']
            assert (t['exit_code'],t['cleanup']['exit_code'])==(0,0)
        else:
            assert d['runtime_error']
            assert (t['exit_code'],t['cleanup']['exit_code'])==(1,2)
        assert d['release_identity']==manifest['release_identity']
        assert d['binary_sha256']==manifest['binary_sha256']
        assert d['affinity']==manifest['affinity'][str(t['cpus'])]
        assert int(d['cgroup_limits']['memory.max'])==manifest['memory_gib']*2**30
        assert d['cgroup_limits']['memory.swap.max']=='0'
        assert d['cgroup_limits']['cpu.max'].split()[0]=='max'
        assert t['cleanup']['remaining_descendants']==t['cleanup']['leaked_descendants']==0
        assert not t['cleanup']['timed_out']
        source=d.get('source',{});lag=d.get('stages_ms',{}).get('commit_to_publication',{})
        records.append(dict(name=t['name'],cpus=t['cpus'],rate=t['rate'],repeat=t['repeat'],status=d['status'],phase=d['phase'],
            runtime_error=d.get('runtime_error'),
            host_contention_flagged=(any(x['at_ms']>=contention['external_build_started_at_ms'] for x in d.get('backlog_series',[])) if contention else None),
            host_iowait_percent=d.get('host',{}).get('cpu_percent',{}).get('iowait'),
            achieved=source.get('achieved_rows_per_second'),
            baseline_rate=d.get('baseline',{}).get('achieved_rows_per_second'),
            warmup_rate=d.get('warmup',{}).get('achieved_rows_per_second'),
            capture_p95_ms=d.get('stages_ms',{}).get('capture_observed_upper_bound',{}).get('p95'),
            admission_p95_ms=d.get('stages_ms',{}).get('commit_to_admission',{}).get('p95'),
            apply_p95_ms=d.get('stages_ms',{}).get('worker_start_to_prepared',{}).get('p95'),
            publication_p95_ms=d.get('stages_ms',{}).get('prepared_to_publication',{}).get('p95'),
            drain_seconds=d.get('drain_seconds'),
            oltp_p95_ms=source.get('transaction_ms',{}).get('p95'),
            baseline_oltp_p95_ms=d.get('baseline',{}).get('transaction_ms',{}).get('p95'),
            lag_p95_ms=lag.get('p95'),lag_p99_ms=lag.get('p99'),
            average_cpu_cores=d.get('cpu',{}).get('average_cpu_cores'),
            peak_memory_mib=round(d.get('peak_memory_bytes',0)/2**20,1) if d.get('peak_memory_bytes') else None,
            peak_backlog_bytes=d.get('peak_backlog_bytes'),last_backlog_bytes=d.get('last_observed_backlog_bytes'),
            observer_busy_samples=d.get('observer_busy_samples'),
            within_5s_p95=d.get('within_5s_p95'),offered_load_met=(source['achieved_rows_per_second']>=t['rate']*.95 if source else None)))
    with (root/'trials.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(records[0]),lineterminator='\n');w.writeheader();w.writerows(records)
    groups=collections.defaultdict(list)
    for d in records:groups[(d['cpus'],d['rate'])].append(d)
    summary=[]
    for (cpus,rate),rows in sorted(groups.items()):
        r=dict(cpus=cpus,rate=rate,trials=len(rows),measured=sum(x['status']=='measured' for x in rows),
            runtime_failed=sum(x['status']=='runtime_failed' for x in rows),
            host_contention_flagged=sum(x['host_contention_flagged'] is True for x in rows),
            within_5s=sum(x['within_5s_p95'] is True for x in rows),
            offered_load_met=sum(x['offered_load_met'] is True for x in rows))
        for key in ('lag_p95_ms','lag_p99_ms','achieved','baseline_rate','average_cpu_cores','oltp_p95_ms','baseline_oltp_p95_ms','peak_memory_mib','observer_busy_samples','capture_p95_ms','admission_p95_ms','apply_p95_ms','publication_p95_ms','drain_seconds'):
            values=[x[key] for x in rows if x[key] is not None]
            r[key]=dict(n=len(values),median=statistics.median(values),minimum=min(values),maximum=max(values)) if values else None
        summary.append(r)
    (root/'summary.json').write_text(json.dumps(dict(aggregation='median and range of trial statistics; not pooled transaction percentiles',groups=summary),indent=2)+'\n')
    return records,summary


def plot(root,records):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'svg.hashsalt':'supabricks-sync-scaling'})
    cpus=sorted({r['cpus'] for r in records});rates=sorted({r['rate'] for r in records})
    colors=['#2563eb','#0891b2','#7c3aed']
    fig,axes=plt.subplots(1,3,figsize=(13,4.6),layout='constrained')
    for i,rate in enumerate(rates):
        color=colors[i%len(colors)]
        for j,cpu in enumerate(cpus):
            rows=[r for r in records if r['cpus']==cpu and r['rate']==rate]
            x=j+(i-(len(rates)-1)/2)*.22
            for axis,key,scale in ((axes[0],'lag_p95_ms',1000),(axes[1],'average_cpu_cores',1)):
                values=[r[key]/scale for r in rows if r[key] is not None]
                if values:
                    middle=statistics.median(values)
                    axis.errorbar(x,middle,yerr=[[middle-min(values)],[max(values)-middle]],fmt='o',capsize=4,color=color,label=f'{rate:,} rows/s' if j==0 else None)
            count=sum(r['status']=='measured' for r in rows)
            axes[2].bar(x,count,width=.2,color=color,label=f'{rate:,} rows/s' if j==0 else None)
            axes[2].text(x,count+.06,f'{count}/{len(rows)}',ha='center',fontsize=8)
    axes[0].axhline(5,ls='--',lw=1,color='#b91c1c');axes[0].set_ylabel('Trial p95 publication lag (seconds; log scale)');axes[0].set_yscale('log');axes[0].set_yticks([5,10,50,100],labels=['5','10','50','100']);axes[0].text(.98,5.2,'5 s target',transform=axes[0].get_yaxis_transform(),ha='right',color='#b91c1c',fontsize=8)
    axes[1].set_ylabel('Average CPU cores used during load');axes[1].set_ylim(0,1.4)
    axes[2].set_ylabel('Complete measurements / trials');axes[2].set_ylim(0,4)
    for ax in axes:
        ax.set_xticks(range(len(cpus)),[str(x) for x in cpus]);ax.set_xlabel('Available logical CPUs (paired SMT)');ax.grid(axis='y',alpha=.15)
    axes[2].legend(loc='upper right',fontsize=8)
    fig.suptitle('Local sync scaling · fixed 16 GiB · 45-second load · three repeats',fontsize=13)
    fig.supxlabel('Median and min–max of available trial statistics. Complete measurements can miss the 5 s target.\nShared-host screening: late external build contention is retained; warmup failures have no load-phase CPU statistic.',fontsize=8)
    fig.savefig(root/'scaling.png',dpi=180);fig.savefig(root/'scaling.svg',metadata={'Date':None})
    svg=root/'scaling.svg';svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines())+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('--plot',action='store_true');a=p.parse_args()
    records,summary=summarize(a.root)
    if a.plot:plot(a.root,records)
    print(json.dumps(summary,indent=2))
