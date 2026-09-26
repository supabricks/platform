"""Recompute SP03a paired summaries; no independent-trial confidence claim."""
import json
from pathlib import Path
import statistics

METRICS = ('lag_p95_ms', 'cpu_cores', 'peak_memory_bytes', 'source_rows_s',
           'capture_transactions_s', 'capture_syncs_per_transaction',
           'capture_durable_ms_per_transaction', 'apply_directory_ms', 'apply_run_ms')


def analyze(root):
    result = {}
    for name in ('main', 'candidate-controls', 'overload-controls'):
        report = json.loads((root / name / 'comparison.json').read_text())
        experiment = json.loads((root / name / 'experiment.json').read_text())
        assert report['complete'] and experiment['state'] == 'complete'
        groups = []
        for group in report['groups']:
            entry = dict(cpus=group['cpus'], offered_rows_s=group['rate'])
            for arm in ('predecessor', 'candidate'):
                entry[arm] = {key: group[arm][key] for key in ('measured', 'failures', 'freshness_passes', 'input_passes')}
                entry[arm]['metrics'] = {key: group[arm]['metrics'][key] for key in METRICS}
            entry['paired_median_change_percent'] = {}
            for metric in METRICS:
                values = [item['percent'] for item in group['paired'][metric] if item['percent'] is not None]
                entry['paired_median_change_percent'][metric] = dict(n=len(values), median=statistics.median(values) if values else None)
            groups.append(entry)
        rejected = [attempt for attempt in experiment['attempts'] if not attempt['accepted']]
        result[name] = dict(accepted_pairs=len(experiment['pairs']), attempts=len(experiment['attempts']),
                            rejected_pairs=len(rejected), retained_rejected_trials=sum(len(a['results']) for a in rejected),
                            groups=groups)
    return result


if __name__ == '__main__':
    print(json.dumps(analyze(Path(__file__).resolve().parent), indent=2, sort_keys=True))
