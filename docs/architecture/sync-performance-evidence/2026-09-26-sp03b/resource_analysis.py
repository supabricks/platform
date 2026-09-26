"""Sampled process CPU attribution; not a replacement for cgroup accounting."""
from collections import defaultdict
import gzip
import json
from pathlib import Path


def analyze(root):
    experiment = json.loads((root / 'experiment.json').read_text())
    assert experiment['state'] == 'complete'
    result = []
    for pair in experiment['pairs']:
        for arm, receipt in pair['results'].items():
            directory = root / receipt['directory']
            trial = json.loads(next(directory.glob('*/trial.json')).read_text())
            profile = json.loads(gzip.decompress(next(directory.glob('*/profile.json.gz')).read_bytes()))
            start, end = trial.get('measurement_start_ms'), trial.get('measurement_end_ms')
            cpu = defaultdict(float)
            seen = {}
            window = [r for r in profile['observations']
                      if start is not None and end is not None and start <= r['at_ms'] <= end]
            for observation in window:
                for process in observation.get('processes', []):
                    identity = (process['pid'], process['created'])
                    previous = seen.get(identity)
                    if previous is None and process['created'] * 1000 >= start:
                        previous = 0
                    if previous is not None:
                        delta = process['cpu_s'] - previous
                        assert delta >= -1e-9
                        cpu[process['role']] += max(0, delta)
                    seen[identity] = process['cpu_s']
            result.append(dict(arm=arm, cpus=pair['pair']['cpus'], rate=pair['pair']['rate'],
                repeat=pair['pair']['repeat'], status=trial['status'],
                cgroup_cpu_cores=receipt['metrics'].get('cpu_cores'),
                process_cpu_seconds=dict(cpu), samples=len(window),
                process_sample_errors=profile.get('process_sample_errors')))
    return dict(scope='Load-window process CPU deltas keyed by PID and creation time. New processes include observed CPU since birth; existing processes start at first in-window sample. Exited-process tails and unobserved short processes are missing. These partial counters do not sum to cgroup CPU and do not prove causation.', trials=result)


if __name__ == '__main__':
    import sys
    print(json.dumps(analyze(Path(sys.argv[1])), indent=2, sort_keys=True))
