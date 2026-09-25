#!/usr/bin/env python3
"""Summarize original and predeclared follow-up 4:50 pairs without replacement."""
import json
from pathlib import Path
import statistics
import sys

root=Path(sys.argv[1])
pairs=[]
for block in ('.','low-load-followup'):
    record=json.loads((root/block/'experiment.json').read_text())
    assert record['state']=='complete'
    for pair in record['pairs']:
        if (pair['pair']['cpus'],pair['pair']['rate'])!=(4,50):
            continue
        row=dict(block=block,repeat=pair['pair']['repeat'],order=pair['pair']['arms'])
        for arm,result in pair['results'].items():
            row[arm]=dict(directory=str(Path(block)/result['directory']),**result['metrics'])
        a,b=row['predecessor']['lag_p95_ms'],row['candidate']['lag_p95_ms']
        row['p95_delta_ms']=b-a if a is not None and b is not None else None
        pairs.append(row)
result=dict(scope='All six 4:50 pairs, including the original freshness miss; no replacement or pooled row percentiles',pairs=pairs)
for arm in ('predecessor','candidate'):
    rows=[p[arm] for p in pairs]
    summary=dict(trials=len(rows),complete=sum(r['status']=='measured' for r in rows),
                 fresh=sum(r.get('within_5s_p95') is True for r in rows),
                 input_met=sum(r.get('offered_load_met') is True for r in rows))
    for key in ('lag_p95_ms','lag_p99_ms','cpu_cores','peak_memory_bytes','capture_commit_ms'):
        values=[r.get(key) for r in rows if r.get(key) is not None]
        summary[key]=dict(n=len(values),median=statistics.median(values) if values else None,
                         minimum=min(values) if values else None,maximum=max(values) if values else None)
    result[arm]=summary
print(json.dumps(result,indent=2,sort_keys=True))
