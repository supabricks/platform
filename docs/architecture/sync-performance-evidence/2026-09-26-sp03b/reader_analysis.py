"""Recompute the separately predeclared read-observer controls."""
import json
from pathlib import Path
import statistics


def describe(values):
    return dict(n=len(values), median=statistics.median(values),
                minimum=min(values), maximum=max(values))


def analyze(root):
    experiment = json.loads((root / 'experiment.json').read_text())
    assert experiment['state'] == 'complete'
    assert len(experiment['pairs']) == 3
    pairs = []
    for pair in experiment['pairs']:
        assert pair['accepted']
        arms = {}
        for receipt in pair['results'].values():
            assert receipt['exit_code'] == 0 and not receipt['overlap']
            trial = json.loads((root / receipt['file']).read_text())
            assert trial['status'] == 'pass' and trial['durability'].startswith('WAL / FULL')
            assert trial['parameters']['profile'] is False
            assert trial['parameters']['reader'] is receipt['reader']
            assert trial['parameters']['single'] is False
            assert trial['parameters']['count'] == 32 and trial['parameters']['age_ms'] == 10
            if not receipt['reader']:
                assert trial['reader']['snapshots'] == 0
            assert trial['journal']['physical_bytes'] <= trial['journal']['physical_limit']
            arms['reader_on' if receipt['reader'] else 'reader_off'] = {
                'transactions_s': trial['transactions_s'],
                'cpu_seconds': trial['cpu_seconds'],
                'physical_bytes': trial['journal']['physical_bytes'],
            }
        changes = {key: (arms['reader_on'][key] / arms['reader_off'][key] - 1) * 100
                   for key in ('transactions_s', 'cpu_seconds', 'physical_bytes')}
        pairs.append(dict(repeat=pair['repeat'], **arms, on_vs_off_percent=changes))
    return dict(
        accepted_pairs=len(pairs), retained_attempts=len(experiment['attempts']), pairs=pairs,
        arms={arm: {metric: describe([p[arm][metric] for p in pairs])
                    for metric in ('transactions_s', 'cpu_seconds', 'physical_bytes')}
              for arm in ('reader_off', 'reader_on')},
        paired_changes_percent={key: describe([p['on_vs_off_percent'][key] for p in pairs])
                                for key in pairs[0]['on_vs_off_percent']},
        scope='Native profiling disabled in both arms. Three pairs screen read-observer overhead; sync counters are unavailable. No source or publication capacity claim.')


if __name__ == '__main__':
    print(json.dumps(analyze(Path(__file__).parent / 'reader-controls'), indent=2, sort_keys=True))
