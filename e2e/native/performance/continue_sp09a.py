#!/usr/bin/env python3
"""Explicit SP09a controller-loss recovery; frozen trials run under user systemd.

No automatic failure retries. Original records are immutable. The unmonitored
pair is repeated only after its cleanup receipt is inspected and verified.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())
def save(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)


def run(a):
    a.output.mkdir(parents=True,exist_ok=False)
    lock=(a.campaign/'.campaign.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    sys.path.insert(0,str(a.harness/'e2e/native/performance'))
    from compare import expected,load_trial,harness_identity,package_identity
    config=read(a.config);original=read(a.campaign/'status.json')
    assert original['config']==config
    assert original['completed']==['component','common-refactor-controls','common-profiler-controls','historical-main']
    assert original['controller_identity']==harness_identity(a.harness)
    state=dict(status='verifying',pid=os.getpid(),heartbeat_ms=time.time()*1000,
        script_sha256=sha(Path(__file__)),original_campaign=str(a.campaign),config_sha256=sha(a.config),
        harness_identity=original['controller_identity'],completed=[],continuation=True)
    def checkpoint(**updates):
        state.update(updates,heartbeat_ms=time.time()*1000);save(a.output/'status.json',state)
    def verify():
        assert sha(Path(__file__))==state['script_sha256']
        assert sha(a.config)==state['config_sha256']
        assert harness_identity(a.harness)==state['harness_identity']
        assert shutil.disk_usage(a.output).free>=64*1024**3
        for arm in config['arms'].values():assert package_identity(Path(arm['release']),arm['revision'])==arm['identity']
    def verify_pairs(path,record):
        for pair in record['pairs']:
            for arm,receipt in pair['results'].items():
                for name,digest in receipt['evidence_sha256'].items():assert sha(path/receipt['directory']/name)==digest
                assert load_trial(path/receipt['directory'],expected(record['config'],arm,pair['pair']))==receipt['metrics']
    def phase(name,cells,clients,seconds,warmup,activation=False,resume=False):
        verify();checkpoint(status='running',phase=name)
        command=[sys.executable,str(a.harness/'e2e/native/performance/compare.py'),
            '--slice','SP09a-'+name,'--hypothesis',config['hypothesis'],'--output',str(a.output/name),
            '--cells',cells,'--clients',str(clients),'--seconds',str(seconds),'--warmup-seconds',str(warmup),
            '--repeats','3','--quiet-seconds','300','--minimum-free-gib','64','--image',config['image']]
        for label,key in [('predecessor','candidate' if activation else 'common'),('candidate','candidate')]:
            arm=config['arms'][key]
            command+=['--'+label+'-release',arm['release'],'--'+label+'-revision',arm['revision'],
                      '--'+label+'-harness',config['workload_harness']]
        if activation:command+=['--activation-control']
        if resume:command+=['--resume']
        with (a.output/(name+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=a.harness)
            checkpoint(child_pid=child.pid)
            while True:
                try:code=child.wait(timeout=10);break
                except subprocess.TimeoutExpired:checkpoint()
        assert code==0,'comparison stopped; investigate '+name
        result=read(a.output/name/'experiment.json');assert result['state']=='complete'
        verify_pairs(a.output/name,result)
        subprocess.run([sys.executable,str(a.harness/'e2e/native/performance/archive_comparison.py'),
            str(a.output/name),str(a.output/'archives'/name)],check=True)
        state['completed'].append(name);checkpoint(status='between_phases',child_pid=None)
    try:
        verify();checkpoint()
        # Require exclusive ownership of every original comparison before audit.
        locks=[]
        for name in original['completed'][1:]+['qualified-main']:
            handle=(a.campaign/name/'.comparison.lock').open('a');fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(handle)
            record=read(a.campaign/name/'experiment.json');verify_pairs(a.campaign/name,record)
        for receipt in read(a.campaign/'component/receipts.json'):
            assert receipt['accepted'] and receipt['exit_code']==0 and not receipt['overlaps']
            for name,digest in receipt['sha256'].items():assert sha(a.campaign/'component'/receipt['directory']/name)==digest
        interrupted=a.campaign/'qualified-main'
        record=read(interrupted/'experiment.json')
        assert record['state']=='running_trial' and len(record['pairs'])==1 and len(record['attempts'])==2
        assert not record['attempts'][-1]['results']
        orphan=interrupted/'02-attempt02-predecessor/01-cpu16-rate1250-r1'
        cleanup=read(orphan/'cleanup.json')
        assert cleanup['exit_code']==cleanup['leaked_descendants']==cleanup['remaining_descendants']==0 and not cleanup['timed_out']
        source_hash=sha(interrupted/'experiment.json')
        shutil.copytree(interrupted,a.output/'qualified-main')
        record['state']='between_pairs'
        record['recovery']=dict(reason='controller_lost; unmonitored pair repeated after verified cleanup',
            original_experiment_sha256=source_hash,orphan_cleanup_sha256=sha(orphan/'cleanup.json'),
            original_directory=str(interrupted),accepted_pairs_retained=1)
        save(a.output/'qualified-main/experiment.json',record)
        assert sha(interrupted/'experiment.json')==source_hash,'original evidence changed'
        save(a.output/'revalidation.json',dict(status='passed',retained_phases=original['completed'],
            qualified_pairs_retained=1,repeated_pair=record['attempts'][-1]['pair'],cleanup=cleanup))
        for handle in locks:handle.close()
        phase('qualified-main','8:1250,16:1250',8,300,60,resume=True)
        phase('historical-profiler-controls','4:50,16:50,8:1000,16:1000',4,45,5,activation=True)
        phase('qualified-profiler-controls','8:1250,16:1250',8,300,60,activation=True)
        checkpoint(status='measurements_complete_review_required',phase='review',completed_at_ms=time.time()*1000)
    except BaseException as error:
        checkpoint(status='stopped_for_investigation',error_type=type(error).__name__,error=str(error));raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','campaign','harness','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args()
    for name,value in vars(a).items():setattr(a,name,value.resolve())
    run(a)
