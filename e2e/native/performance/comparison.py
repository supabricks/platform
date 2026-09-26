"""Validate paired trial evidence and report marginal changes without survivor bias."""
import gzip
import json
import math
from pathlib import Path
import statistics

from matrix import accepted

METRICS = ('lag_p95_ms', 'lag_p99_ms', 'source_rows_s', 'baseline_rows_s', 'cpu_cores',
           'peak_memory_bytes', 'peak_backlog_bytes', 'last_backlog_bytes',
           'capture_transactions_s', 'capture_commit_ms', 'capture_syncs_per_commit',
           'capture_transactions_per_group', 'capture_syncs_per_transaction', 'capture_durable_ms_per_transaction',
           'apply_directory_ms', 'apply_merge_ms', 'apply_run_ms')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def compressed_json(path, limit=256*1024**2):
    with gzip.open(path, 'rb') as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'profile decompressed budget exceeded')
    return json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def median(values):
    values = [x for x in values if x is not None]
    return statistics.median(values) if values else None


def describe(values):
    values = [x for x in values if x is not None]
    return dict(n=len(values), median=median(values), minimum=min(values), maximum=max(values)) if values else dict(n=0, median=None, minimum=None, maximum=None)


def delta(before, after):
    if before is None or after is None:
        return dict(absolute=None, percent=None)
    return dict(absolute=after-before, percent=100*(after-before)/before if before else None)


def profile_metrics(profile, trial):
    workers = profile.get('workers', {})
    require(not profile.get('monitor_errors'), 'profile monitor failed')
    require('daemon.jsonl' in workers and workers['daemon.jsonl'], 'missing daemon profile')
    require(workers['daemon.jsonl'][-1].get('final'), 'missing daemon final snapshot')
    for prefix in ('capture-', 'incremental-'):
        require(any(k.startswith(prefix) for k in workers), 'missing worker profile: ' + prefix)
    tails = []
    for name, rows in workers.items():
        require(isinstance(rows, list) and rows, 'empty profile stream')
        previous = -1
        for row in rows:
            require(isinstance(row.get('at_ms'), (int, float)) and math.isfinite(row['at_ms']) and row['at_ms'] >= previous, 'profile clock regressed')
            previous = row['at_ms']
            require(not row.get('budget_exceeded') and not row.get('profile_write_errors'), 'profile output failed')
            if name != 'daemon.jsonl':
                require(row.get('native_io') is not None, 'native sync counters missing')
        if not rows[-1].get('final'):
            tails.append(name)
    start, end = trial.get('measurement_start_ms'), trial.get('measurement_end_ms')
    result = dict(incomplete_worker_tails=tails, worker_streams=len(workers),
                  capture_window=None,
                  counter_capabilities=dict(capture_groups='existing one-transaction append / COMMIT counters',
                                            checkpoints='not separately instrumented in predecessor',
                                            journal_retries='not implemented in predecessor'))
    if any('journal_reads' in batch for batch in profile.get('batches', [])):
        result['counter_capabilities']['journal_retries']='bounded operational counters in batches.journal_reads; all worker attempts retained'
    if start is None or end is None:
        return result
    tx = commits = ns = syncs = duration = groups = 0
    groups_available=False
    apply = []
    for name, rows in workers.items():
        if name.startswith('capture-'):
            window = [r for r in rows if start <= r['at_ms'] <= end]
            if len(window) < 2:
                continue
            first, last = window[0], window[-1]
            def difference(section, key, field):
                a = first.get(section, {}).get(key, {}).get(field, 0)
                b = last.get(section, {}).get(key, {}).get(field, 0)
                require(b >= a, 'profile cumulative counter regressed')
                return b-a
            tx += difference('work', 'capture.spool.append.transactions', 'total')
            if 'capture.spool.append.groups' in last.get('work',{}):
                groups_available=True
                groups+=difference('work','capture.spool.append.groups','total')
            commits += difference('metrics', 'sqlite.COMMIT', 'calls')
            ns += difference('metrics', 'sqlite.COMMIT', 'total_ns')
            syncs += sum(difference('work', 'sqlite.COMMIT.'+op+'.calls', 'total') for op in ('fsync', 'fdatasync'))
            duration += (last['at_ms']-first['at_ms'])/1000
        if name.startswith('incremental-') and start <= rows[0].get('started_at_ms', -1) <= end:
            last = rows[-1]
            run = last.get('metrics', {}).get('apply.run', {})
            if run.get('calls') and not run.get('errors'):
                apply.append({key: last['metrics'].get(stage, {}).get('total_ns', 0)/1e6
                              for key, stage in [('apply_directory_ms', 'apply.boundary'),
                                                 ('apply_merge_ms', 'apply.delta_merge'),
                                                 ('apply_run_ms', 'apply.run')]})
    result.update(capture_transactions_s=tx/duration if duration else None,
                  capture_commit_ms=ns/commits/1e6 if commits else None,
                  capture_syncs_per_commit=syncs/commits if commits else None,
                  capture_transactions_per_group=tx/groups if groups_available and groups else None,
                  capture_syncs_per_transaction=syncs/tx if tx else None,
                  capture_durable_ms_per_transaction=ns/tx/1e6 if tx else None,
                  capture_window=dict(seconds=duration, transactions=tx, commits=commits,groups=groups if groups_available else None),
                  successful_apply_workers=len(apply),
                  **{key: median([r[key] for r in apply]) for key in ('apply_directory_ms', 'apply_merge_ms', 'apply_run_ms')})
    if groups_available:result['counter_capabilities']['capture_groups']='durable append group and transaction counters; COMMIT totals also include metadata/pruning'
    return result


def validate_trial(manifest, entry, trial, cleanup, expected):
    require(accepted(entry), 'measurement or cleanup failed')
    require(cleanup == entry.get('cleanup'), 'cleanup receipt differs')
    require(trial['status'] == entry['status'], 'trial status differs')
    for key in ('release_identity', 'binary_sha256'):
        require(trial[key] == manifest[key] == expected[key], 'changed ' + key)
    require(manifest['image_id'] == expected['image_id'], 'changed qualifier image')
    require(manifest['runtime_revision'] == expected['runtime_revision'], 'changed runtime revision')
    if 'harness_identity' in expected:
        identity = expected['harness_identity']
        require(manifest['harness_revision'] == identity['revision'] and not manifest['harness_dirty'], 'changed harness revision')
        for name, checksum in manifest['harness_sha256'].items():
            require(identity['files'].get('e2e/native/performance/'+name) == checksum, 'harness file differs: '+name)
    for key in ('cpus', 'rate'):
        require(entry[key] == expected[key], 'wrong trial cell')
    require(trial['affinity'] == manifest['affinity'][str(expected['cpus'])] == expected['affinity'], 'wrong affinity')
    require(manifest['memory_gib'] == expected['memory_gib'], 'wrong memory envelope')
    limits = trial['cgroup_limits']
    require(int(limits['memory.max']) == expected['memory_gib']*2**30 and limits['memory.swap.max'] == '0'
            and limits['cpu.max'].split()[0] == 'max', 'wrong cgroup envelope')
    for key, value in expected['parameters'].items():
        require(trial['parameters'].get(key) == value, 'changed workload: ' + key)
    checks = trial.get('checks', [])
    require('owned_runtime_stopped' in checks, 'missing stopped-runtime receipt')
    status = trial['status']
    source = trial.get('source', {})
    lag = trial.get('stages_ms', {}).get('commit_to_publication', {})
    if status == 'measured':
        require('both_published_tables_equal_frozen_postgres_source' in checks, 'missing final correctness')
        require(source.get('completed_transactions', 0) > 0, 'missing completed source transactions')
        require(trial.get('observed_transactions', 0) >= source['completed_transactions'], 'missing transaction markers')
        require(all(isinstance(lag.get(k), (int, float)) and math.isfinite(lag[k]) and lag[k] >= 0
                    for k in ('p50', 'p95', 'p99', 'maximum')), 'missing or invalid complete latency')
        require(lag['p50'] <= lag['p95'] <= lag['p99'] <= lag['maximum'], 'invalid percentile ordering')
        require(trial.get('within_5s_p95') == (lag['p95'] <= 5000), 'freshness result differs')
        require(trial.get('offered_load_met') == (source['achieved_rows_per_second'] >= expected['rate']*.95), 'input-rate result differs')
    else:
        require(status == 'runtime_failed' and trial.get('runtime_error'), 'unknown failure outcome')
        require(not lag, 'incomplete trial cannot supply complete latency')
    if source:
        count, elapsed = source['completed_transactions'], source['elapsed_seconds']
        require(elapsed > 0 and count >= 0, 'invalid source accounting')
        # Existing reports round elapsed/rate to milliseconds / 0.001 rows/s.
        require(abs(source['achieved_rows_per_second']-2*count/elapsed) <= max(.1, .001*expected['rate']), 'source rate differs from counts')
        require(source['target_transactions'] == count+source['unsent_transactions'], 'source transaction accounting differs')
        require(source['target_rows_per_second'] == expected['rate'], 'source target differs')
    return dict(status=status, runtime_error=trial.get('runtime_error'), phase=trial.get('phase'),
                lag_p95_ms=lag.get('p95'), lag_p99_ms=lag.get('p99'),
                source_rows_s=source.get('achieved_rows_per_second'),
                baseline_rows_s=trial.get('baseline', {}).get('achieved_rows_per_second'),
                cpu_cores=trial.get('cpu', {}).get('average_cpu_cores'),
                peak_memory_bytes=trial.get('peak_memory_bytes'),
                peak_backlog_bytes=trial.get('peak_backlog_bytes'),
                last_backlog_bytes=trial.get('last_observed_backlog_bytes'),
                within_5s_p95=trial.get('within_5s_p95'), offered_load_met=trial.get('offered_load_met'))


def load_trial(root, expected):
    root = Path(root)
    archive = compressed_json(root/'raw-reports.json.gz') if (root/'raw-reports.json.gz').exists() else None
    manifest = archive['matrix'] if archive else read_json(root/'matrix.json')
    require(manifest.get('completed_at') and len(manifest['trials']) == len(manifest['order']) == 1, 'incomplete single-trial matrix')
    entry = manifest['trials'][0]
    require(all(entry[k] == manifest['order'][0][k] for k in ('cpus', 'rate', 'repeat')), 'matrix order differs')
    if archive:
        receipt = archive['trials'][entry['name']]
        trial, cleanup = receipt['trial'], receipt['cleanup']
        profile_path = root/'profile.json.gz'
    else:
        trial_root = root/entry['name']
        trial, cleanup = read_json(trial_root/'trial.json'), read_json(trial_root/'cleanup.json')
        profile_path = trial_root/'profile.json.gz'
    result = validate_trial(manifest, entry, trial, cleanup, expected)
    if expected['parameters']['profile']:
        require(trial.get('profile') and profile_path.exists(), 'missing profile evidence')
        result.update(profile_metrics(compressed_json(profile_path), trial))
    return result


def aggregate(rows):
    return dict(trials=len(rows), measured=sum(r['status'] == 'measured' for r in rows),
                failures=sum(r['status'] == 'runtime_failed' for r in rows),
                freshness_passes=sum(r.get('within_5s_p95') is True for r in rows),
                input_passes=sum(r.get('offered_load_met') is True for r in rows),
                metrics={key: describe([r.get(key) for r in rows]) for key in METRICS})


def compare_records(pairs, historical=()):
    groups = []
    for cell in sorted({(p['cpus'], p['rate']) for p in pairs}):
        rows = [p for p in pairs if (p['cpus'], p['rate']) == cell]
        before, after = ([p[arm] for p in rows] for arm in ('predecessor', 'candidate'))
        prior = [p for p in historical if (p['cpus'], p['rate']) == cell]
        a, b, old = aggregate(before), aggregate(after), aggregate(prior)
        paired = {key: [dict(repeat=p['repeat'], **delta(p['predecessor'].get(key), p['candidate'].get(key))) for p in rows] for key in METRICS}
        complete_pair_set = all(p['predecessor']['status'] == p['candidate']['status'] == 'measured' for p in rows)
        complete_history = bool(prior) and all(p['status'] == 'measured' for p in prior) and b['measured'] == b['trials']
        groups.append(dict(cpus=cell[0], rate=cell[1], predecessor=a, candidate=b, historical=old,
                           marginal={k: delta(a['metrics'][k]['median'], b['metrics'][k]['median']) if not k.startswith('lag_') or complete_pair_set else delta(None,None) for k in METRICS},
                           original_baseline={k: delta(old['metrics'][k]['median'], b['metrics'][k]['median']) if not k.startswith('lag_') or complete_history else delta(None,None) for k in METRICS},
                           paired=paired,
                           outcome_transitions=[dict(repeat=p['repeat'],before=p['predecessor']['status'],after=p['candidate']['status']) for p in rows]))
    return dict(interpretation='Trial-level medians/ranges and paired deltas, not pooled percentiles. Missing/failing outcomes never become latency samples; historical differences are not controlled causal effects.', groups=groups, pairs=pairs)
