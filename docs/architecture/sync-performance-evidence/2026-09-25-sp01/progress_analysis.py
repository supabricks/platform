#!/usr/bin/env python3
"""Retain whole-fixture progress and load-window resource evidence for accepted trials."""
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
experiment = json.loads((root / 'experiment.json').read_text())
rows = []
for pair in experiment['pairs']:
    for arm, result in pair['results'].items():
        paths = list((root / result['directory']).rglob('trial.json'))
        assert len(paths) == 1
        trial = json.loads(paths[0].read_text())
        rows.append(dict(
            arm=arm, cpus=pair['pair']['cpus'], rate=pair['pair']['rate'],
            repeat=pair['pair']['repeat'], directory=result['directory'],
            status=trial['status'], phase=trial.get('phase'),
            runtime_error=trial.get('runtime_error'),
            fixture_and_cleanup_seconds=(result['ended_at_ms']-result['started_at_ms'])/1000,
            source=trial.get('source'),
            baseline=trial.get('baseline'),
            observed_transactions=trial.get('observed_transactions'),
            observed_publications=trial.get('observed_publications'),
            drain_seconds=trial.get('drain_seconds'),
            drain_timeout_evidence=trial.get('drain_timeout_evidence'),
            peak_backlog_bytes=trial.get('peak_backlog_bytes'),
            last_observed_backlog_bytes=trial.get('last_observed_backlog_bytes'),
            cgroup_before=trial.get('cgroup_before'), cgroup_after=trial.get('cgroup_after'),
            cpu=trial.get('cpu'), checks=trial.get('checks'),
        ))
(root / 'progress-analysis.json').write_text(json.dumps(dict(
    scope='Accepted trials only. Source and cgroup counters describe the measured load window when present. Fixture duration includes startup, warmup, drain and cleanup, not just time to failure. Drain timeout evidence is a post-stop capture lower bound, not successful end-to-end throughput. Missing phases/counters are null.',
    trials=rows), indent=2, sort_keys=True)+'\n')
