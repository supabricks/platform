"""Trial-level paired summaries including explicit WAL checkpoint costs."""
import json
from pathlib import Path
import statistics

from wal_analysis import analyze


METRICS = (
    'commit_and_checkpoint_syncs_per_transaction',
    'all_native_syncs_per_transaction',
    'commit_and_checkpoint_ms_per_transaction',
    'commit_and_checkpoint_sync_ms_per_transaction',
    'all_native_sync_ms_per_transaction',
    'max_physical_bytes', 'max_wal_bytes',
)


def describe(values):
    return dict(n=len(values), median=statistics.median(values) if values else None,
                minimum=min(values) if values else None,
                maximum=max(values) if values else None)


def summarize(experiments):
    result = {}
    for name, trials in experiments.items():
        groups = []
        for cpus, rate in sorted({(r['cpus'], r['rate']) for r in trials}):
            selected = [r for r in trials if (r['cpus'], r['rate']) == (cpus, rate)]
            arms = {arm: [r for r in selected if r['arm'] == arm]
                    for arm in ('predecessor', 'candidate')}
            pairs = []
            for repeat in sorted({r['repeat'] for r in selected}):
                before, = [r for r in arms['predecessor'] if r['repeat'] == repeat]
                after, = [r for r in arms['candidate'] if r['repeat'] == repeat]
                changes = {}
                for key in METRICS:
                    a, b = before.get(key), after.get(key)
                    changes[key] = dict(absolute=b-a if a is not None and b is not None else None,
                                        percent=(b/a-1)*100 if a and b is not None else None)
                pairs.append(dict(repeat=repeat, changes=changes))
            groups.append(dict(cpus=cpus, offered_rows_s=rate,
                arms={arm: {key: describe([r[key] for r in rows if r.get(key) is not None])
                            for key in METRICS} for arm, rows in arms.items()},
                pairs=pairs,
                paired_changes={key: {kind: describe([p['changes'][key][kind] for p in pairs
                                                     if p['changes'][key][kind] is not None])
                                      for kind in ('absolute', 'percent')} for key in METRICS}))
        result[name] = groups
    return result


if __name__ == '__main__':
    print(json.dumps(summarize(analyze(Path(__file__).parent)), indent=2, sort_keys=True))
