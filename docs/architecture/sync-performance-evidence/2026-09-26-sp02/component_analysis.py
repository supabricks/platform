"""Recompute component screening medians/ranges from individual retained receipts."""
import collections,json,statistics,sys
from pathlib import Path

def analyze(root):
    manifest=json.loads((root/'screen.json').read_text());groups=collections.defaultdict(list)
    assert manifest['state']=='complete'
    for attempt in manifest['trials']:
        if attempt['overlap'] or attempt['exit_code']:continue
        trial=json.loads((root/attempt['file']).read_text());assert trial['status']=='pass'
        c=trial['parameters'];key=tuple(c[n] for n in ('count','age_ms','offered_transactions_s','reader','single'))
        groups[key].append(trial)
    summary=[]
    for key,rows in sorted(groups.items()):
        assert len(rows)==3
        entry=dict(config=dict(zip(('count','age_ms','offered_transactions_s','reader','single'),key)))
        for name in ('transactions_s','transactions_per_group','syncs_per_transaction','sync_ms_per_transaction','cpu_seconds'):
            values=[r[name] for r in rows]
            entry[name]=dict(n=len(values),values=values,median=statistics.median(values),minimum=min(values),maximum=max(values))
        summary.append(entry)
    return dict(trials=len(manifest['trials']),contended=sum(bool(t['overlap']) for t in manifest['trials']),groups=summary)

if __name__=='__main__':print(json.dumps(analyze(Path(sys.argv[1])),indent=2))
