#!/usr/bin/env python3
"""Summarize retained SP04 profiles; checkpoint-inclusive costs are in wal_analysis."""
import argparse
import gzip
import json
from pathlib import Path
import statistics


def describe(values):
    return dict(n=len(values), median=statistics.median(values), minimum=min(values), maximum=max(values)) if values else dict(n=0, median=None, minimum=None, maximum=None)


def analyze(root):
    record=json.loads((root/'experiment.json').read_text())
    assert record['state']=='complete', 'experiment is incomplete'
    rows=[]
    for pair in record['pairs']:
        for arm, result in pair['results'].items():
            directory=root/result['directory']
            trial_path,=directory.glob('*/trial.json')
            trial=json.loads(trial_path.read_text())
            row=dict(arm=arm, **pair['pair'], status=trial['status'],
                     missing_status_samples=trial.get('observer_missing_status_samples'),
                     busy_samples=trial.get('observer_busy_samples'),
                     stages_ms=trial.get('stages_ms'),source_sql_ms=trial.get('source',{}).get('source_sql_ms'),capture=[])
            row.pop('arms')
            profile_path=trial_path.with_name('profile.json.gz')
            if profile_path.exists():
                profile=json.load(gzip.open(profile_path))
                successful_apply=dict(workers=0,lifecycle_cpu_seconds=0,journal_transactions=0)
                for name,snapshots in profile['workers'].items():
                    if name.startswith('incremental-'):
                        start,end=trial.get('measurement_start_ms'),trial.get('measurement_end_ms')
                        last=snapshots[-1];run=last['metrics'].get('apply.run',{})
                        if start is not None and end is not None and start<=snapshots[0]['started_at_ms']<=end and run.get('calls') and not run.get('errors'):
                            successful_apply['workers']+=1
                            successful_apply['lifecycle_cpu_seconds']+=last['cpu_user_s']+last['cpu_system_s']
                            successful_apply['journal_transactions']+=last['work'].get('apply.journal.transactions',{}).get('total',0)
                    if not name.startswith('capture-'):continue
                    gauges=[s['capture_cursors'] for s in snapshots if s.get('capture_cursors')]
                    comparable=[g for g in gauges if g.get('feedback') is not None and g.get('durable') is not None]
                    assert all(g['feedback']<=g['durable'] for g in comparable), 'feedback beyond observed durable cursor'
                    work=snapshots[-1].get('work',{})
                    wait=work.get('capture.groups.flush.accumulation_ms',{})
                    commit=work.get('capture.groups.flush.commit_ms',{})
                    start,end=trial.get('measurement_start_ms'),trial.get('measurement_end_ms')
                    window=[v for v in snapshots if start is not None and end is not None and start<=v['at_ms']<=end]
                    load=None
                    if len(window)>=2:
                        first,last=window[0],window[-1]
                        def diff(key,field='total'):
                            value=last.get('work',{}).get(key,{}).get(field,0)-first.get('work',{}).get(key,{}).get(field,0)
                            assert value>=0, 'cumulative counter regressed'
                            return value
                        transactions=diff('capture.spool.append.transactions')
                        groups=diff('capture.spool.append.groups')
                        payload=diff('capture.spool.append.payload_bytes')
                        seconds=(last['at_ms']-first['at_ms'])/1000
                        sync_ms=sum(diff('sqlite.COMMIT.'+op+'.total_ns') for op in ('fsync','fdatasync'))/1e6
                        load=dict(seconds=seconds,transactions=transactions,groups=groups,payload_bytes=payload,
                            payload_bytes_s=payload/seconds,payload_bytes_per_group=payload/groups if groups else None,
                            native_commit_sync_ms_per_transaction=sync_ms/transactions if transactions else None,
                            capture_cpu_seconds=last['cpu_user_s']+last['cpu_system_s']-first['cpu_user_s']-first['cpu_system_s'])
                    row['capture'].append(dict(stream=name,snapshots=len(snapshots),load_window=load,
                        final=snapshots[-1].get('final'),comparable_cursor_samples=len(comparable),
                        feedback_beyond_durable=0,last_cursors=gauges[-1] if gauges else None,
                        accumulation_ms_mean=wait.get('total',0)/wait['count'] if wait.get('count') else None,
                        accumulation_ms_maximum=wait.get('maximum'),
                        commit_ms_mean=commit.get('total',0)/commit['count'] if commit.get('count') else None,
                        commit_ms_maximum=commit.get('maximum'),
                        maximum_group_transactions=work.get('capture.spool.append.transactions',{}).get('maximum'),
                        maximum_group_payload_bytes=work.get('capture.spool.append.payload_bytes',{}).get('maximum')))
                row['successful_apply_workers_started_in_load']=successful_apply
            rows.append(row)
    groups=[]
    for cpus,rate,arm in sorted({(r['cpus'],r['rate'],r['arm']) for r in rows}):
        selected=[r for r in rows if (r['cpus'],r['rate'],r['arm'])==(cpus,rate,arm)]
        stages=sorted({k for r in selected for k in (r['stages_ms'] or {})})
        groups.append(dict(cpus=cpus,rate=rate,arm=arm,trials=len(selected),
            stage_trial_p95_ms={k:describe([r['stages_ms'][k]['p95'] for r in selected if r['stages_ms'] and k in r['stages_ms']]) for k in stages},
            capture={k:describe([c[k] for r in selected for c in r['capture'] if c[k] is not None]) for k in ('accumulation_ms_mean','accumulation_ms_maximum','commit_ms_mean','commit_ms_maximum','maximum_group_transactions','maximum_group_payload_bytes')},
            successful_apply_workers_started_in_load={k:describe([r['successful_apply_workers_started_in_load'][k] for r in selected if 'successful_apply_workers_started_in_load' in r]) for k in ('workers','lifecycle_cpu_seconds','journal_transactions')},
            source_sql_trial_p95_ms={k:describe([r['source_sql_ms'][k]['p95'] for r in selected if r['source_sql_ms']]) for k in ('begin','updates','xid','commit')},
            load_window={k:describe([c['load_window'][k] for r in selected for c in r['capture'] if c['load_window'] and c['load_window'][k] is not None]) for k in ('payload_bytes_s','payload_bytes_per_group','native_commit_sync_ms_per_transaction','capture_cpu_seconds')},
            comparable_cursor_samples=sum(c['comparable_cursor_samples'] for r in selected for c in r['capture']),
            missing_status_samples=[r['missing_status_samples'] for r in selected]))
    return dict(scope='All-run capture statistics and sampled cursor invariants; stage statistics are medians/ranges of complete trial p95s, never pooled percentiles. Missing outcomes remain missing. Successful apply-worker CPU covers full lifetimes of completed workers started during load, can cross load edges and excludes failed workers; it does not sum to cgroup load CPU. Sampling is not a durability proof.',groups=groups,trials=rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment',type=Path)
    args=parser.parse_args()
    print(json.dumps(analyze(args.experiment),indent=2,sort_keys=True))
