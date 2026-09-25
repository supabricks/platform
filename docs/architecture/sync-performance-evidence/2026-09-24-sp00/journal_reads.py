#!/usr/bin/env python3
"""Recompute journal-read diagnostics from a completed paired evidence archive."""
import gzip
import json
from pathlib import Path
import sys

root=Path(sys.argv[1])
record=json.loads((root/'experiment.json').read_text())
result=[]
for pair in record['pairs']:
    for arm, receipt in pair['results'].items():
        directory=root/receipt['directory']
        matrix=json.loads((directory/'matrix.json').read_text())
        trial_root=directory/matrix['trials'][0]['name']
        trial=json.loads((trial_root/'trial.json').read_text())
        profile=json.loads(gzip.decompress((trial_root/'profile.json.gz').read_bytes()))
        workers=[]
        for name,snapshots in profile['workers'].items():
            if not name.startswith('incremental-'):
                continue
            final=snapshots[-1]
            metrics=final['metrics']
            select=metrics.get('sqlite.SELECT',{})
            run=metrics.get('apply.run',{})
            workers.append(dict(stream=name, started_at_ms=final['started_at_ms'],final=final['final'],
                select_ms=select.get('total_ns',0)/1e6,
                select_max_ms=select.get('max_ns',0)/1e6,
                select_errors=select.get('errors',0),
                journal_ms=metrics.get('apply.journal',{}).get('total_ns',0)/1e6,
                run_ms=run.get('total_ns',0)/1e6,run_errors=run.get('errors',0),
                boundary_ms=metrics.get('apply.boundary',{}).get('total_ns',0)/1e6,
                exceptions=final.get('exceptions',[])))
        result.append(dict(cell={k:pair['pair'][k] for k in ('cpus','rate','repeat')},arm=arm,
            directory=receipt['directory'],status=trial['status'],runtime_error=trial.get('runtime_error'),
            phase=trial.get('phase'),within_5s_p95=trial.get('within_5s_p95'),
            stages_ms=trial.get('stages_ms'),measurement_start_ms=trial.get('measurement_start_ms'),
            measurement_end_ms=trial.get('measurement_end_ms'),
            longest_reads=sorted(workers,key=lambda r:r['select_ms'],reverse=True)[:3],
            longest_apply=sorted(workers,key=lambda r:r['run_ms'],reverse=True)[:3],
            failed_read_workers=sum(r['select_errors']>0 for r in workers)))
print(json.dumps(result,indent=2,sort_keys=True))
