#!/usr/bin/env python3
"""Screen SP09b's CPU-bound prerequisite using retained SP09a request profiles.

No workloads or runtime changes. Request CPU is a per-request delta, never a sum
of repeated process-cumulative snapshots. Durability spans overlap parent spans.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import statistics


def describe(values):
    values=sorted(values)
    if not values:return None
    return dict(count=len(values),minimum=values[0],median=statistics.median(values),
                p95=values[max(0,(95*len(values)+99)//100-1)],maximum=values[-1])


def screen(root):
    trials=[]
    for phase in ('historical-main','qualified-main'):
        folder=root/phase
        record=json.loads((folder/'experiment.json').read_text())
        assert record['state']=='complete'
        for pair in record['pairs']:
            receipt=pair['results']['candidate'];trial_dir=folder/receipt['directory']
            for name,digest in receipt['evidence_sha256'].items():
                path=trial_dir/name
                assert path.resolve().is_relative_to(trial_dir.resolve())
                assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
            profile_path=next(trial_dir.glob('*/profile.json.gz'))
            trial=json.loads(profile_path.with_name('trial.json').read_text())
            assert trial['status']=='measured'
            profile=json.loads(gzip.decompress(profile_path.read_bytes()))
            rows=[];excluded=dict(outside_window=0,cold=0,failed_or_incomplete=0)
            for name,snapshots in profile['workers'].items():
                if not name.startswith('incremental-'):continue
                row=snapshots[-1];metrics=row['metrics'];run=metrics.get('apply.run',{})
                if not trial['measurement_start_ms']<=row['started_at_ms']<trial['measurement_end_ms']:
                    excluded['outside_window']+=1;continue
                if not row.get('final') or not run.get('calls') or run.get('errors'):
                    excluded['failed_or_incomplete']+=1;continue
                if row['worker_request_index']==1:
                    excluded['cold']+=1;continue
                assert row['counter_scope']=='process_cumulative'
                wall=run['total_ns']/1e6;cpu=row['request_cpu_s']*1000
                assert wall>0 and cpu>=0
                def ms(label):return metrics.get(label,{}).get('total_ns',0)/1e6
                rows.append(dict(apply_wall_ms=wall,request_cpu_ms=cpu,request_cpu_per_apply_wall=cpu/wall,
                    fsync_wall_ms=ms('durability.fsync'),fsync_per_apply_wall=ms('durability.fsync')/wall,
                    table_wall_ms=ms('apply.apply_table'),merge_wall_ms=ms('apply.delta_merge'),
                    planning_wall_ms=ms('apply.plan'),table_calls=metrics.get('apply.apply_table',{}).get('calls',0)))
            assert rows
            trials.append(dict(phase=phase,cell=pair['pair'],profile=str(profile_path.relative_to(root)),
                profile_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
                excluded=excluded,metrics={key:describe([r[key] for r in rows]) for key in rows[0]},
                whole_stack_cpu_cores=receipt['metrics']['cpu_cores'],
                source_rows_s=receipt['metrics']['source_rows_s']))
    return dict(scope='Successful warm requests starting in measurement window; tails can cross its end. '
        'CPU includes all process threads and small profiling wrapper overhead. Fsync is Python fsync wall time; '
        'native durability is not fully represented. Inclusive table/merge/fsync spans overlap and must not be added. '
        'Two-table reference workload only; this is a prerequisite screen, not a concurrency speedup measurement.',
        source_root=str(root),script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),trials=trials)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('evidence',type=Path)
    args=parser.parse_args();print(json.dumps(screen(args.evidence),indent=2))
