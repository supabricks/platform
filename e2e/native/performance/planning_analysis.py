#!/usr/bin/env python3
"""SP04 attribution; nonoverlapping directory costs and nested metadata counts."""
import gzip
import json
from pathlib import Path
import statistics
import sys


def describe(values):
    return dict(n=len(values),median=statistics.median(values),minimum=min(values),maximum=max(values)) if values else dict(n=0,median=None,minimum=None,maximum=None)
def change(a,b):return 100*(b-a)/a if a else None
def costs(row):
    metrics=row['metrics'];work=row['work']
    value=lambda name:metrics.get(name,{}).get('total_ns',0)/1e6
    count=lambda name:work.get(name,{}).get('total',0)
    # These three spans do not nest in each other. Other stages/run do nest.
    checks=sum(value(k) for k in ('apply.boundary','apply.planning_inventory','apply.planning_check'))
    result=dict(directory_ms=checks,plan_ms=value('apply.plan'),run_ms=value('apply.run'),
        inventory_ms=value('apply.inventory'),retention_ms=value('apply.retained_boundary'),durability_ms=value('apply.durable'),
        verify_previous_ms=value('apply.verify_previous'),delta_merge_ms=value('apply.delta_merge'))
    for scope in ('plan','run'):
        for name in ('recursive_walks','stat_calls','lstat_calls','fstat_calls','statvfs_calls','scandir_calls'):
            result[scope+'_'+name]=count('apply.'+scope+'.python_'+name)
    return result


def component(root):
    screen=json.loads((root/'screen.json').read_text());rows=[]
    for pair in screen['pairs']:
        for arm,entry in pair['arms'].items():
            raw=json.loads((root/entry['result']).read_text())
            rows.append(dict(index=pair['index'],attempt=pair['attempt'],accepted=pair['accepted'],arm=arm,
                age=raw['age'],keys=raw['keys'],scan_batches=raw['scan_batches'],files=raw['files'],
                wall_ms=raw['wall_seconds']*1000,cpu_ms=raw['cpu_seconds']*1000,**costs(raw)))
    groups=[]
    for age,keys in sorted({(r['age'],r['keys']) for r in rows}):
        selected=[r for r in rows if r['accepted'] and (r['age'],r['keys'])==(age,keys)]
        metrics=('wall_ms','cpu_ms','directory_ms','plan_recursive_walks','plan_stat_calls','plan_lstat_calls','plan_fstat_calls','plan_scandir_calls')
        group=dict(age=age,keys=keys,arms={arm:{key:describe([r[key] for r in selected if r['arm']==arm]) for key in metrics} for arm in ('predecessor','candidate')})
        paired={}
        for key in metrics:
            deltas=[]
            for index in {r['index'] for r in selected}:
                a=next(r for r in selected if r['index']==index and r['arm']=='predecessor')
                b=next(r for r in selected if r['index']==index and r['arm']=='candidate')
                d=change(a[key],b[key])
                if d is not None:deltas.append(d)
            paired[key]=describe(deltas)
        group['paired_change_percent']=paired;groups.append(group)
    return dict(groups=groups,trials=rows)


def fullstack(root):
    experiment=json.loads((root/'experiment.json').read_text());assert experiment['state']=='complete'
    trials=[]
    for pair in experiment['pairs']:
        for arm,result in pair['results'].items():
            path,=(root/result['directory']).glob('*/trial.json');trial=json.loads(path.read_text());profile=path.with_name('profile.json.gz')
            if not profile.exists():continue
            profile=json.load(gzip.open(profile));workers=[];excluded=[]
            for name,snapshots in profile['workers'].items():
                if not name.startswith('incremental-'):continue
                last=snapshots[-1];run=last['metrics'].get('apply.run',{})
                if not (trial['measurement_start_ms']<=snapshots[0]['started_at_ms']<=trial['measurement_end_ms']):continue
                if not run.get('calls') or run.get('errors'):
                    excluded.append(dict(stream=name,run=run,final=last.get('final')));continue
                workers.append(dict(stream=name,**costs(last)))
            metrics=sorted({k for r in workers for k in r if k!='stream'})
            trials.append(dict(arm=arm,cpus=pair['pair']['cpus'],rate=pair['pair']['rate'],repeat=pair['pair']['repeat'],
                trial=str(path.relative_to(root)),status=trial['status'],workers=workers,excluded=excluded,
                worker_medians={key:statistics.median([r[key] for r in workers]) for key in metrics}))
    groups=[]
    for cpus,rate,arm in sorted({(r['cpus'],r['rate'],r['arm']) for r in trials}):
        selected=[r for r in trials if (r['cpus'],r['rate'],r['arm'])==(cpus,rate,arm)]
        metrics=sorted({k for r in selected for k in r['worker_medians']})
        groups.append(dict(cpus=cpus,rate=rate,arm=arm,trial_worker_medians={key:describe([r['worker_medians'][key] for r in selected if key in r['worker_medians']]) for key in metrics}))
    return dict(scope='Completed successful workers started in load; lifetimes may cross load boundaries. Nested stages/counters overlap; never sum parent and child. Directory time includes boundary plus new planning inventory/check spans. Metadata counts are Python APIs only, not native Arrow/Delta syscalls. Failed/incomplete workers are listed separately.',groups=groups,trials=trials)

if __name__=='__main__':print(json.dumps(dict(component=component,fullstack=fullstack)[sys.argv[1]](Path(sys.argv[2])),indent=2,sort_keys=True))
