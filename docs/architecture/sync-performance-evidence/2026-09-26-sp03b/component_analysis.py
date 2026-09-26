"""Recompute the fixed SP03b component factorial and observer controls."""
import json
from pathlib import Path
import statistics


def summary(values):
    return dict(n=len(values),median=statistics.median(values),minimum=min(values),maximum=max(values))


def analyze(root):
    manifest=json.loads((root/'screen.json').read_text())
    assert manifest['state']=='complete'
    accepted={};rejected=[]
    for receipt in manifest['trials']:
        assert receipt['exit_code']==0
        if receipt['overlap']:rejected.append(receipt);continue
        key=json.dumps(receipt['trial'],sort_keys=True)
        assert key not in accepted
        value=json.loads((root/receipt['file']).read_text())
        assert value['status']=='pass' and value['durability'].startswith(receipt['trial']['config']['journal_mode'].upper()+' / FULL')
        assert value['journal']['physical_bytes']<=value['journal']['physical_limit']
        accepted[key]=(receipt,value)
    assert len(accepted)==30
    groups={}
    for receipt,value in accepted.values():
        config=receipt['trial']['config'];key=(receipt['trial']['kind'],config['journal_mode'],config['rate'],config['single'],config['profile'])
        groups.setdefault(key,[]).append(value)
    result=[]
    for (kind,mode,rate,single,profile),values in sorted(groups.items()):
        assert len(values)==3
        metrics={key:summary([v[key] for v in values]) for key in ('transactions_s','cpu_seconds','transactions_per_group')}
        if profile:
            metrics.update({key:summary([v[key] for v in values]) for key in ('syncs_per_transaction','sync_ms_per_transaction')})
        metrics['checkpoint_ms']=summary([v['journal']['checkpoint_ms'] for v in values])
        metrics['checkpoint_busy']=summary([v['journal']['busy'] for v in values])
        metrics['physical_bytes']=summary([v['journal']['physical_bytes'] for v in values])
        result.append(dict(kind=kind,journal_mode=mode,offered_transactions_s=rate,grouping=not single,profile=profile,metrics=metrics))
    return dict(accepted_trials=len(accepted),retained_contended_trials=len(rejected),groups=result,
                scope='Synthetic durable capture including pruning/checkpoints; excludes PostgreSQL source, apply and publication. Observer-disabled sync counters are unavailable, not zero.')


if __name__=='__main__':
    import sys
    print(json.dumps(analyze(Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).parent/'component-screen'),indent=2,sort_keys=True))
