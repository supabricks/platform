#!/usr/bin/env python3
"""SP09a startup screen from retained, successful measured SP08 requests."""
import gzip
import hashlib
import json
from pathlib import Path
import statistics
import sys


def screen(root):
    trials=[]
    for phase in ('historical-main','qualified-main'):
        for path in sorted((root/phase).glob('*candidate/*/profile.json.gz')):
            trial=json.loads(path.with_name('trial.json').read_text());profile=json.load(gzip.open(path));rows=[];excluded=0
            for name,snapshots in profile['workers'].items():
                if not name.startswith('incremental-'):continue
                row=snapshots[-1]
                if not trial['measurement_start_ms']<=row['started_at_ms']<trial['measurement_end_ms']:continue
                metrics=row['metrics'];run=metrics.get('apply.run',{})
                if not run.get('calls') or run.get('errors'):excluded+=1;continue
                imports=metrics['startup.imports']['total_ns']/1e6;apply=run['total_ns']/1e6
                rows.append(dict(imports_ms=imports,apply_ms=apply,imports_fraction=imports/(imports+apply)))
            trials.append(dict(phase=phase,profile=str(path.relative_to(root)),profile_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                successful_requests=len(rows),excluded_failed_or_incomplete=excluded,
                medians={key:statistics.median(r[key] for r in rows) for key in rows[0]}))
    return dict(scope='Successful workers started during measurement; their lifetimes may cross the boundary. Import timing is directly instrumented; fraction covers imports plus apply, not end-to-end latency. No performance improvement claim.',trials=trials)

if __name__=='__main__':print(json.dumps(screen(Path(sys.argv[1])),indent=2))
