"""Offline SP05 triage of immutable SP04 profiles; no new measurements or speedup."""
import gzip
import hashlib
import json
from pathlib import Path
import statistics


def load(path):
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes())


def describe(values):
    return dict(n=len(values), median=statistics.median(values), minimum=min(values), maximum=max(values)) if values else dict(n=0, median=None, minimum=None, maximum=None)


def verify(root):
    for manifest in [root/'SHA256SUMS', root/'main/SHA256SUMS']:
        for line in manifest.read_text().splitlines():
            expected, name = line.split('  ', 1)
            path = (manifest.parent/name).resolve()
            assert path.is_relative_to(root.resolve()), name
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name


def window(rows, start, end):
    selected = [r for r in rows if start <= r['at_ms'] <= end]
    assert len(selected) >= 2, 'insufficient in-load profile snapshots'
    return selected[0], selected[-1]


def delta(first, last, stage, field):
    value = last['metrics'].get(stage, {}).get(field, 0) - first['metrics'].get(stage, {}).get(field, 0)
    assert value >= 0, (stage, field)
    return value


def analyze(root):
    verify(root)
    experiment = load(root/'main/experiment.json')
    assert experiment['state'] == 'complete'
    trials = []
    apply_stages = ('startup.imports', 'apply.run', 'apply.inventory', 'apply.verify_previous',
                    'apply.retained_boundary', 'apply.durable', 'apply.maintenance.base', 'apply.maintenance.compact')
    for pair in experiment['pairs']:
        assert pair['accepted']
        for arm, receipt in pair['results'].items():
            directory = root/'main'/receipt['directory']
            path, = directory.glob('*/trial.json')
            trial = load(path); profile = load(path.with_name('profile.json.gz'))
            assert trial['status'] == 'measured'
            start, end = trial['measurement_start_ms'], trial['measurement_end_ms']
            capture_rows = [r for name, r in profile['workers'].items() if name.startswith('capture-')]
            assert len(capture_rows) == 1, 'new capture lifetimes require separate attribution'
            capture_rows, = capture_rows
            first, last = window(capture_rows, start, end)
            covered_ms = last['at_ms'] - first['at_ms']
            capture = {}
            for stage in ('sqlite.WAL_CHECKPOINT', 'capture.spool.prune', 'capture.spool.progress', 'sqlite.COMMIT', 'sqlite.DELETE'):
                metric = dict(calls=delta(first,last,stage,'calls'), errors=delta(first,last,stage,'errors'),
                    elapsed_ms=delta(first,last,stage,'total_ns')/1e6,
                    lifetime_max_ms=capture_rows[-1]['metrics'].get(stage,{}).get('max_ns',0)/1e6)
                metric['covered_wall_percent'] = 100*metric['elapsed_ms']/covered_ms
                capture[stage] = metric
            journals = [s['capture_journal'] for s in trial['backlog_series'] if s.get('capture_journal')]
            assert journals
            for j in journals:
                assert j['physical_bytes'] == sum(j[k] for k in ('database_bytes','wal_bytes','shm_bytes','rollback_journal_bytes'))
                assert j['physical_bytes'] <= j['physical_limit'] and j['synchronous'] == 2
            storage = dict(max_physical_bytes=max(j['physical_bytes'] for j in journals),
                max_wal_bytes=max(j['wal_bytes'] for j in journals),
                max_budget_percent=max(100*j['physical_bytes']/j['physical_limit'] for j in journals),
                max_busy_counter=max(j['busy'] for j in journals),
                max_backpressure_counter=max(j['backpressure_events'] for j in journals),
                samples=len(journals))
            workers=[];excluded=[];all_compactions=0
            for name, rows in profile['workers'].items():
                if not name.startswith('incremental-'):continue
                metrics=rows[-1]['metrics'];all_compactions+=metrics.get('apply.maintenance.compact',{}).get('calls',0)
                if not start <= rows[0]['started_at_ms'] <= end:continue
                run=metrics.get('apply.run',{})
                if not run.get('calls') or run.get('errors'):
                    excluded.append(name);continue
                workers.append({stage:dict(total_ms=metrics.get(stage,{}).get('total_ns',0)/1e6,
                    max_ms=metrics.get(stage,{}).get('max_ns',0)/1e6,
                    calls=metrics.get(stage,{}).get('calls',0)) for stage in apply_stages})
            assert workers
            apply = {stage:dict(per_worker_ms=describe([w[stage]['total_ms'] for w in workers]),
                maximum_call_ms=max(w[stage]['max_ms'] for w in workers),
                calls=sum(w[stage]['calls'] for w in workers)) for stage in apply_stages}
            daemon_rows=profile['workers']['daemon.jsonl'];df,dl=window(daemon_rows,start,end)
            daemon={stage:dict(calls=delta(df,dl,stage,'calls'), elapsed_ms=delta(df,dl,stage,'total_ns')/1e6,
                lifetime_max_ms=daemon_rows[-1]['metrics'][stage]['max_ns']/1e6)
                for stage in ('publication.verify','publication.file_fsync','publication.descriptor','publication.commit','publication.tick')}
            trials.append(dict(arm=arm,cpus=pair['pair']['cpus'],rate=pair['pair']['rate'],repeat=pair['pair']['repeat'],
                source_trial=str(path.relative_to(root)),capture_covered_ms=covered_ms,capture=capture,storage=storage,
                completed_workers=len(workers),excluded_workers=excluded,apply=apply,all_recorded_compaction_calls=all_compactions,
                daemon=daemon,daemon_covered_ms=dl['at_ms']-df['at_ms'],
                p95_lag_ms=trial['stages_ms']['commit_to_publication']['p95']))
    assert len(trials) == 24
    assert {(t["cpus"],t["rate"],t["arm"]) for t in trials} == {(c,r,a) for c,r in ((4,50),(16,50),(8,1000),(16,1000)) for a in ("predecessor","candidate")}
    groups=[]
    for cpu,rate,arm in sorted({(t['cpus'],t['rate'],t['arm']) for t in trials}):
        selected=[t for t in trials if (t['cpus'],t['rate'],t['arm'])==(cpu,rate,arm)]
        assert len(selected)==3
        groups.append(dict(cpus=cpu,rate=rate,arm=arm,
            capture={stage:{key:describe([t['capture'][stage][key] for t in selected]) for key in selected[0]['capture'][stage]} for stage in selected[0]['capture']},
            storage={key:describe([t['storage'][key] for t in selected]) for key in selected[0]['storage']},
            apply={stage:dict(trial_median_worker_ms=describe([t['apply'][stage]['per_worker_ms']['median'] for t in selected]),
                trial_maximum_call_ms=describe([t['apply'][stage]['maximum_call_ms'] for t in selected])) for stage in apply_stages},
            all_recorded_compaction_calls=sum(t['all_recorded_compaction_calls'] for t in selected),
            daemon={stage:{key:describe([t['daemon'][stage][key] for t in selected]) for key in selected[0]['daemon'][stage]} for stage in selected[0]['daemon']}))
    return dict(scope='Offline analysis of all 24 existing SP04 main trials. No new runtime/harness/observer or paired performance result. Capture/daemon deltas use first/last snapshots inside load; coverage excludes edges. Maxima cover full recorded lifetime, not just load. Apply lifetimes may cross load edges. Nested spans overlap (especially checkpoint/prune, durability/verification/publication); never sum them or infer a throughput bound. Missing compact calls mean unexercised rotation, not zero-cost compaction. No sustained plateau inference from 45-second trials.',
        source_manifest_sha256=hashlib.sha256((root/'SHA256SUMS').read_bytes()).hexdigest(),trials=trials,groups=groups)


if __name__ == '__main__':
    print(json.dumps(analyze(Path(__file__).resolve().parent.parent/'2026-09-26-sp04'),indent=2,sort_keys=True))
