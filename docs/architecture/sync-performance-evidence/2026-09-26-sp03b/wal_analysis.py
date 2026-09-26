"""Recompute checkpoint-inclusive capture work and physical/feedback invariants."""
import gzip
import json
from pathlib import Path
import statistics


def load(path):
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix=='.gz' else path.read_text())


def analyze_trial(root,result):
    directory=root/result['directory']
    trial_path=next(directory/p for p in result['evidence_sha256'] if p.endswith('/trial.json'))
    trial=load(trial_path);start=trial.get('measurement_start_ms');end=trial.get('measurement_end_ms')
    samples=trial.get('backlog_series',[])
    journals=[s['capture_journal'] for s in samples if s.get('capture_journal')]
    for j in journals:
        assert j['physical_bytes']==sum(j[k] for k in ('database_bytes','wal_bytes','shm_bytes','rollback_journal_bytes'))
        assert j['physical_bytes']<=j['physical_limit'] and j['synchronous']==2
    value=dict(status=trial['status'],profile=trial['parameters']['profile'],journal_samples=len(journals),
               max_physical_bytes=max((j['physical_bytes'] for j in journals),default=None),
               max_wal_bytes=max((j['wal_bytes'] for j in journals),default=None),
               max_checkpoint_busy_counter=max((j['busy'] for j in journals),default=None),
               max_backpressure_counter=max((j['backpressure_events'] for j in journals),default=None))
    if not value['profile']:return value
    profile_path=next(directory/p for p in result['evidence_sha256'] if p.endswith('/profile.json.gz'))
    profile=load(profile_path);totals=dict(transactions=0,seconds=0,commit_syncs=0,checkpoint_syncs=0,all_native_syncs=0,checkpoint_calls=0,checkpoint_ms=0,commit_ms=0,commit_sync_ms=0,checkpoint_sync_ms=0,all_native_sync_ms=0,all_native_sync_errors=0)
    feedback_checks=0
    for name,rows in profile['workers'].items():
        if not name.startswith('capture-'):continue
        for row in rows:
            cursors=row.get('capture_cursors') or {}
            if cursors.get('durable') is not None and cursors.get('feedback') is not None:
                assert cursors['feedback']<=cursors['durable'];feedback_checks+=1
        window=[r for r in rows if start is not None and end is not None and start<=r['at_ms']<=end]
        if len(window)<2:continue
        first,last=window[0],window[-1]
        def delta(section,key,field):
            difference=last.get(section,{}).get(key,{}).get(field,0)-first.get(section,{}).get(key,{}).get(field,0)
            assert difference>=0
            return difference
        totals['seconds']+=(last['at_ms']-first['at_ms'])/1000
        totals['transactions']+=delta('work','capture.spool.append.transactions','total')
        totals['checkpoint_calls']+=delta('metrics','sqlite.WAL_CHECKPOINT','calls')
        for stage,label in [('commit','sqlite.COMMIT'),('checkpoint','sqlite.WAL_CHECKPOINT')]:
            totals[stage+'_ms']+=delta('metrics',label,'total_ns')/1e6
            totals[stage+'_syncs']+=sum(delta('work',label+'.'+op+'.calls','total') for op in ('fsync','fdatasync'))
            totals[stage+'_sync_ms']+=sum(delta('work',label+'.'+op+'.total_ns','total') for op in ('fsync','fdatasync'))/1e6
        totals['all_native_syncs']+=sum(delta('native_io',op,'calls') for op in ('fsync','fdatasync'))
        totals['all_native_sync_ms']+=sum(delta('native_io',op,'total_ns') for op in ('fsync','fdatasync'))/1e6
        totals['all_native_sync_errors']+=sum(delta('native_io',op,'errors') for op in ('fsync','fdatasync'))
    value.update(capture_window=totals,feedback_invariant_samples=feedback_checks)
    tx=totals['transactions']
    if tx:
        value.update(commit_syncs_per_transaction=totals['commit_syncs']/tx,
                     checkpoint_syncs_per_transaction=totals['checkpoint_syncs']/tx,
                     commit_and_checkpoint_syncs_per_transaction=(totals['commit_syncs']+totals['checkpoint_syncs'])/tx,
                     all_native_syncs_per_transaction=totals['all_native_syncs']/tx,
                     commit_and_checkpoint_ms_per_transaction=(totals['commit_ms']+totals['checkpoint_ms'])/tx,
                     commit_and_checkpoint_sync_ms_per_transaction=(totals['commit_sync_ms']+totals['checkpoint_sync_ms'])/tx,
                     all_native_sync_ms_per_transaction=totals['all_native_sync_ms']/tx)
    return value


def analyze(root):
    result={}
    for name in ('main','predecessor-controls','candidate-controls','delete-ablation','wal-ablation'):
        path=root/name;experiment=load(path/'experiment.json');assert experiment['state']=='complete'
        trials=[]
        for pair in experiment['pairs']:
            assert pair['accepted']
            for arm,receipt in pair['results'].items():
                trials.append(dict(arm=arm,**{k:pair['pair'][k] for k in ('cpus','rate','repeat')},**analyze_trial(path,receipt)))
        result[name]=trials
    return result


if __name__=='__main__':
    print(json.dumps(analyze(Path(__file__).resolve().parent),indent=2,sort_keys=True))
