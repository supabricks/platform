#!/usr/bin/env python3
"""Nine fresh, sequential baseline/DATE/CHAR scalar controls; no SP campaign."""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import time
from inputs import sha


def run(a):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',a.image):raise ValueError('immutable container image identity required')
    repo=Path(__file__).resolve().parents[2];root=a.output.resolve();root.mkdir()
    releases={k:getattr(a,k).resolve() for k in ('baseline','date','char')}
    for path in releases.values():
        if not path.is_relative_to(repo):raise ValueError('release must be under repository for read-only container mount')
    identity={k:sha(p/'release.json') for k,p in releases.items()}
    (root/'host-processes.txt').write_text(subprocess.check_output(['ps','-eo','pid,ppid,comm,pcpu,rss'],text=True))
    fixture=sha(Path(__file__).with_name('composite.py'));receipts=[]
    orders=[('baseline','date','char'),('char','baseline','date'),('date','char','baseline')]
    for n,order in enumerate(orders,1):
        for arm in order:
            name=f'control-{n}-{arm}';out=root/name;out.mkdir()
            command=['docker','run','--rm','--name','supabricks-eq171-'+name,'--network','none',
                '--cpuset-cpus','0-7','--memory','16g','--memory-swap','16g','--user',f'{os.getuid()}:{os.getgid()}',
                '--mount',f'type=bind,src={repo},dst=/repo,readonly',
                '--mount',f'type=bind,src={out},dst=/reports','-w','/repo',a.image,
                'python3','install/native/catalog_gate.py','--timeout','600','--report','/reports/cleanup.json','--',
                'python3','e2e/tpcds/composite.py','--control-only','--release','/repo/'+str(releases[arm].relative_to(repo)),
                '--report','/reports/result.json']
            (out/'command.json').write_text(json.dumps(command,indent=2)+'\n')
            print('START',name,flush=True);started=time.monotonic()
            with (out/'output.log').open('w') as log:
                result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=660)
            receipt=dict(trial=name,arm=arm,exit_code=result.returncode,elapsed=time.monotonic()-started)
            receipts.append(receipt);(root/'campaign.json').write_text(json.dumps(receipts,indent=2)+'\n')
            print('END',name,result.returncode,flush=True)
            if result.returncode:raise SystemExit(result.returncode)
            report=json.loads((out/'result.json').read_text());cleanup=json.loads((out/'cleanup.json').read_text())
            assert report['status']=='PASS' and report['release_identity']==identity[arm] and report['fixture_sha256']==fixture
            assert cleanup['exit_code']==0 and not cleanup['timed_out'] and cleanup['leaked_descendants']==cleanup['remaining_descendants']==0
            assert len(report['metrics']['control_samples'])==12
    summary={}
    for arm in releases:
        trials=[json.loads((root/f'control-{n}-{arm}/result.json').read_text()) for n in range(1,4)]
        per_trial=[[s['ack_to_observed_publication_ms'] for s in t['metrics']['control_samples']] for t in trials]
        samples=sum(per_trial,[])
        commits=[s['commit_ms'] for t in trials for s in t['metrics']['control_samples']]
        summary[arm]=dict(identity=identity[arm],trials=3,transactions=len(samples),mean_publication_ms=sum(samples)/len(samples),
            per_trial_mean_publication_ms=[sum(s)/len(s) for s in per_trial],max_publication_ms=max(samples),
            mean_commit_ms=sum(commits)/len(commits))
    result=dict(status='PASS',scope='short unchanged scalar control; descriptive only, no sustained throughput claim',fixture_sha256=fixture,arms=summary)
    (root/'summary.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('baseline','date','char','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--image',required=True);run(p.parse_args())
