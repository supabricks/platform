#!/usr/bin/env python3
"""SP10c supervised sequence: observer bridge, engine/product, sustained fixtures.

No automatic adoption. Preserve failed phases and stop on validation errors.
Every matrix uses the existing frozen campaign and its bounded contention policy.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from compare import ROOT,harness_identity,package_identity,sha
from host_monitor import HostMonitor
from matrix import affinity,topology


def save(path,data):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(data,indent=2)+'\n');temporary.replace(path)


def run(path,output):
    config=json.loads(path.read_text());fingerprint=sha(path);identity=harness_identity(ROOT)
    output.mkdir(exist_ok=False);lock=(output/'.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=dict(status='starting',pid=os.getpid(),config_sha256=fingerprint,controller=identity,completed=[])
    def checkpoint(**kwargs):state.update(kwargs,heartbeat_ms=time.time()*1000);save(output/'status.json',state)
    def verify():
        assert sha(path)==fingerprint and harness_identity(ROOT)==identity
        for arm in config['arms'].values():assert package_identity(Path(arm['release']),arm['revision'])==arm['identity']
    def execute(name,command):
        verify();checkpoint(status='running',phase=name)
        with (output/(name+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT)
            checkpoint(child_pid=child.pid)
            while True:
                try:code=child.wait(timeout=10);break
                except subprocess.TimeoutExpired:checkpoint()
        checkpoint(child_pid=None)
        assert code==0,(name,code)
    try:
        for phase in config['campaigns']:
            phase_config=Path(phase['config']);assert sha(phase_config)==phase['sha256']
            execute(phase['name'],[sys.executable,str(ROOT/'e2e/native/performance/backend_campaign.py'),
                '--config',str(phase_config),'--output',str(output/phase['name'])])
            result=json.loads((output/phase['name']/'status.json').read_text())
            assert result['status']=='measurements_complete_review_required'
            state['completed'].append(phase['name']);checkpoint(status='between_phases')
        folder=output/'sustained';folder.mkdir();monitor=HostMonitor(folder).start()
        receipts=[]
        try:
            for arm_name in config['sustained_order']:
                # One 30-minute fresh fixture per arm; descriptive sustained
                # qualification, not a repeated population speedup estimate.
                import concurrent.futures
                checkpoint(status='waiting_for_quiet',phase='sustained-'+arm_name)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pending=pool.submit(monitor.wait_quiet)
                    while True:
                        try:quiet=pending.result(timeout=10);break
                        except concurrent.futures.TimeoutError:checkpoint()
                arm=config['arms'][arm_name];report=folder/arm_name;report.mkdir()
                command=['docker','run','--rm','--init','--name','sp10c-sustained-'+str(os.getpid()),
                    '--network','none','--cpuset-cpus',','.join(map(str,affinity(topology(),8))),
                    '--memory','16g','--memory-swap','16g','--user',f'{os.getuid()}:{os.getgid()}',
                    '-v',str(ROOT)+':/repo:ro','-v',arm['release']+':/release:ro','-v',str(report)+':/reports',
                    '-w','/repo',config['image'],'python3','install/native/catalog_gate.py','--timeout','2400',
                    '--report','/reports/cleanup.json','--','python3','e2e/native/performance/sustained_journal.py',
                    '--release','/release','--scratch','/reports','--report','/reports/result.json']
                begin=time.time()*1000
                try:execute('sustained-'+arm_name,command)
                finally:
                    end=time.time()*1000
                    receipts.append(dict(arm=arm_name,quiet=quiet,started_at_ms=begin,ended_at_ms=end,
                        overlaps=monitor.overlap(begin,end),files={p.name:sha(p) for p in report.iterdir() if p.is_file()}))
                    save(folder/'receipts.json',receipts)
                assert not receipts[-1]['overlaps'],'sustained host contention: retain and review, do not silently replace'
                data=json.loads((report/'result.json').read_text());cleanup=json.loads((report/'cleanup.json').read_text())
                assert data['status']=='measured' and data['offered_load_met'] and data['within_5s_p95']
                assert data['parameters']['seconds']>=1800 and not data['parameters'].get('screen')
                assert cleanup['exit_code']==cleanup['leaked_descendants']==cleanup['remaining_descendants']==0 and not cleanup['timed_out']
                assert json.loads((report/'reopen.json').read_text())
                state['completed'].append('sustained-'+arm_name);checkpoint(status='between_phases')
        finally:monitor.close()
        checkpoint(status='measurements_complete_review_required',phase='manual_review')
    except BaseException as error:
        checkpoint(status='stopped_for_investigation',error_type=type(error).__name__,error=str(error));raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.config.resolve(),a.output.resolve())
