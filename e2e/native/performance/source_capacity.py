#!/usr/bin/env python3
"""Frozen SP06 source screen/observer controls; sequential, retained whole blocks."""
import argparse
import fcntl
import gzip
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

from compare import harness_identity, package_identity, save, sha
from host_monitor import HostMonitor
from matrix import affinity, output, topology

ROOT=Path(__file__).resolve().parents[3]


def blocks(mode,cpus,clients,repeats,seed):
    result=[]
    for cpu in cpus:
        for repeat in range(1,repeats+1):
            if mode=='controls':
                variants=[dict(clients=clients[0],profile=False),dict(clients=clients[0],profile=True)]
                if repeat%2:variants.reverse()
            else:
                ordered=clients[(repeat-1)%len(clients):]+clients[:(repeat-1)%len(clients)]
                variants=[dict(clients=c,profile=True) for c in ordered]
            result.append(dict(cpus=cpu,repeat=repeat,variants=variants))
    random.Random(seed).shuffle(result)
    return result


def validate_report(report,cleanup,config,block,variant):
    assert report['status']=='measured' and cleanup['exit_code']==0
    assert not cleanup['timed_out'] and cleanup['leaked_descendants']==cleanup['remaining_descendants']==0
    assert report['release_identity']==config['package']['release_identity']
    assert report['binary_sha256']==config['package']['binary_sha256']
    assert report['affinity']==config['affinity'][str(block['cpus'])]
    for k,v in dict(config['parameters'],**variant).items():assert report['parameters'][k]==v,k
    limits=report['cgroup_limits']
    assert limits['memory.max']==str(16*1024**3) and limits['memory.swap.max']=='0'
    assert limits['cpu.max'].split()[0]=='max'
    assert set(report['checks'])=={'no_capture_or_apply_policy','both_source_tables_equal_acknowledged_generator_state','owned_runtime_stopped'}
    source=report['source']
    assert source['measurement_seconds']==config['parameters']['seconds']
    assert source['committed_transactions']==source['in_window_transactions']+source['tail_transactions']
    assert source['committed_changed_rows_s']==2*source['in_window_transactions']/source['measurement_seconds']
    assert sum(w['transactions'] for w in source['windows'])==source['in_window_transactions']
    assert source['tail_transactions']<=variant['clients']
    if variant['profile']:assert 'profile' in report


def run(args):
    args.output.mkdir(parents=True,exist_ok=args.resume)
    lock=(args.output/'.controller.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    harness=ROOT;release=args.release.resolve();groups=topology()
    config=dict(mode=args.mode,harness=str(harness),harness_identity=harness_identity(harness),
        release=str(release),package=package_identity(release,args.runtime_revision),
        image_id=output('docker','image','inspect',args.image,'--format','{{.Id}}'),topology=groups,
        affinity={str(c):affinity(groups,c) for c in args.cpus},
        parameters=dict(rate=args.rate,seconds=args.seconds,warmup=args.warmup,rows=10000),
        memory_gib=16,quiet_seconds=300,seed=args.seed,
        order=blocks(args.mode,args.cpus,args.clients,args.repeats,args.seed),
        filesystem=json.loads(output('findmnt','--json','-T',str(args.output),'-o','SOURCE,FSTYPE,OPTIONS')))
    record=dict(state='between_blocks',config=config,attempts=[],blocks=[])
    manifest=args.output/'experiment.json'
    if args.resume:
        record=json.loads(manifest.read_text());assert record['config']==config
        assert record['state'] in ('between_blocks','waiting','complete'),'investigate interrupted trial cleanup before resume'
        for block in record['blocks']:
            assert block['accepted']
            for entry in block['results']:
                for name,digest in entry['evidence_sha256'].items():assert sha(args.output/entry['directory']/name)==digest
        if record['state']=='complete':return
    def persist():save(manifest,record)
    def identities():
        assert harness_identity(harness)==config['harness_identity']
        assert package_identity(release,args.runtime_revision)==config['package']
        assert topology()==config['topology']
        assert output('docker','image','inspect',config['image_id'],'--format','{{.Id}}')==config['image_id']
    monitor=HostMonitor(args.output).start();persist()
    try:
        for index,block in enumerate(config['order']):
            if index<len(record['blocks']):continue
            prior=[a for a in record["attempts"] if a["index"]==index]
            for old in prior:
                if len(old["results"])<len(block["variants"]):old["reason"]="interrupted_between_trials; retained and replaced as a whole block"
            first_attempt=max((a["attempt"] for a in prior),default=0)+1
            for attempt in range(first_attempt,4):
                attempt_record=dict(index=index,block=block,attempt=attempt,accepted=False,results=[])
                record['attempts'].append(attempt_record);persist()
                for variant in block['variants']:
                    record['state']='waiting';persist();quiet=monitor.wait_quiet();identities()
                    name=f'{index+1:02}-attempt{attempt}-cpu{block["cpus"]}-clients{variant["clients"]}-profile{int(variant["profile"])}'
                    dest=args.output/name;dest.mkdir();scratch=dest/'scratch';scratch.mkdir()
                    container=f'sb-source-{os.getpid()}-{index}-{attempt}-{variant["clients"]}-{int(variant["profile"])}'
                    command=['docker','run','--rm','--init','--name',container,'--network','none',
                        '--cpuset-cpus',','.join(map(str,config['affinity'][str(block['cpus'])])),
                        '--memory','16g','--memory-swap','16g','--user',f'{os.getuid()}:{os.getgid()}',
                        '-v',f'{harness}:/repo:ro','-v',f'{release}:/release:ro','-v',f'{dest.resolve()}:/reports','-v',f'{scratch.resolve()}:/scratch',
                        '-w','/repo',config['image_id'],'python3','install/native/catalog_gate.py','--timeout','600','--report','/reports/cleanup.json','--',
                        'python3','e2e/native/performance/source_trial.py','--release','/release','--report','/reports/trial.json','--scratch','/scratch',
                        '--clients',str(variant['clients']),*sum(([f'--{k}',str(v)] for k,v in config['parameters'].items()),[])]
                    if variant['profile']:command.append('--profile')
                    record['state']='running';persist();start=time.time()*1000
                    print('TRIAL_START',name,flush=True)
                    with (dest/'private.log').open('w') as log:
                        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
                        try:
                            deadline=time.monotonic()+660
                            while child.poll() is None:
                                monitor.check()
                                if time.monotonic()>deadline:raise TimeoutError('source fixture timeout')
                                time.sleep(1)
                        except BaseException:
                            subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                            child.wait(timeout=15);raise
                    end=time.time()*1000;target=time.monotonic()
                    while monitor.last_sample<=target:monitor.check();time.sleep(.1)
                    identities()
                    entry=dict(directory=name,variant=variant,start_ms=start,end_ms=end,quiet=quiet,
                        overlap=monitor.overlap(start,time.time()*1000),exit_code=child.returncode,
                        evidence_sha256={p.name:sha(p) for p in dest.iterdir() if p.is_file() and (p.suffix=='.json' or p.suffix=='.gz')})
                    attempt_record['results'].append(entry);persist()
                    assert child.returncode==0,'source trial failed; retained, never silently rerun'
                    report=json.loads((dest/'trial.json').read_text());cleanup=json.loads((dest/'cleanup.json').read_text())
                    validate_report(report,cleanup,config,block,variant)
                    samples=json.load(gzip.open(dest/'source-samples.json.gz'))['samples']
                    from source_trial import summarize
                    computed=summarize(samples,config['parameters']['seconds'],report['source']['elapsed_seconds'])
                    assert all(report['source'][k]==v for k,v in computed.items()),'source accounting mismatch'
                    assert sha(dest/'source-samples.json.gz')==report['samples']['sha256']
                    print('TRIAL_COMPLETE',name,report['source']['committed_changed_rows_s'],flush=True)
                attempt_record['accepted']=not any(r['overlap'] for r in attempt_record['results'])
                record['state']='between_blocks'
                if attempt_record['accepted']:record['blocks'].append(attempt_record)
                persist()
                if attempt_record['accepted']:break
                print('CONTENDED_BLOCK_RETAINED',index,attempt,flush=True)
            else:raise RuntimeError('contention replacement budget exhausted')
        record['state']='complete';persist()
    finally:monitor.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release',type=Path,required=True);p.add_argument('--runtime-revision',required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--mode',choices=('screen','controls','envelope'),required=True)
    p.add_argument('--clients',type=int,nargs='+',default=[4,8,16]);p.add_argument('--cpus',type=int,nargs='+',default=[8,16])
    p.add_argument('--repeats',type=int,default=3);p.add_argument('--rate',type=int,default=10000)
    p.add_argument('--seconds',type=int,default=300);p.add_argument('--warmup',type=int,default=60)
    p.add_argument('--seed',type=int,default=20260926);p.add_argument('--image',default='supabricks-sy08-qualifier:latest')
    p.add_argument('--resume',action='store_true');a=p.parse_args()
    if min(*a.clients,*a.cpus,a.repeats,a.rate,a.seconds,a.warmup)<1 or max(a.clients)>10000:p.error('invalid dimensions')
    if a.mode=='controls' and len(a.clients)!=1:p.error('controls require one selected client count')
    run(a)
