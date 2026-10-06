#!/usr/bin/env python3
"""Frozen SP11 steady-load campaign. Stops on failed evidence; never resumes in place."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from compare import ROOT, harness_identity, package_identity, sha
from host_monitor import HostMonitor
from matrix import affinity, topology


def save(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)


def stop_interrupted(child,container,completed):
    # The Docker client can exit before its daemon-owned container. An interrupted
    # wait must stop the named container even when poll() already reports exit.
    if not completed:
        subprocess.run(['docker','stop','--time','30',container],capture_output=True,timeout=45)
        if child.poll() is None:child.wait(timeout=30)


def run(config_path,output):
    config=json.loads(config_path.read_text());fingerprint=sha(config_path)
    identity=harness_identity(ROOT);machine=topology()
    if identity!=config['harness_identity']:raise ValueError('frozen harness differs')
    if machine!=config['topology']:raise ValueError('host topology differs')
    output.mkdir(exist_ok=False)
    lock=(output/'.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    state=dict(status='starting',pid=os.getpid(),completed=[],config_sha256=fingerprint,harness=identity,
               scope='SP11 steady-load baseline only; other SP11 gates remain outstanding')
    monitor=HostMonitor(output,quiet_seconds=config['quiet_seconds'],publish_quiet=True).start()
    def checkpoint(**values):
        state.update(values,heartbeat_ms=time.time()*1000);save(output/'status.json',state)
    def verify():
        monitor.check()
        if sha(config_path)!=fingerprint or harness_identity(ROOT)!=identity:raise ValueError('campaign source changed')
        arm=config['runtime']
        if package_identity(Path(arm['release']),arm['revision'])!=arm['identity']:raise ValueError('runtime identity changed')
        if shutil.disk_usage(output).free<config['minimum_free_gib']*2**30:raise ValueError('insufficient free disk')
        image=subprocess.check_output(['docker','image','inspect',config['image'],'--format','{{.Id}}'],text=True).strip()
        if image!=config['image']:raise ValueError('qualifier image differs')
    try:
        for index,step in enumerate(config['order'],1):
            verify();name=f"{index:02}-cpu{step['cpus']}-{step['seconds']}s-r{step['repeat']}"
            checkpoint(status='waiting_for_quiet',phase=name)
            # A continuous monitor retains quiet credit across phase boundaries.
            waiting=time.monotonic()
            while True:
                try:quiet=monitor.wait_quiet(max_wait=10);break
                except TimeoutError:
                    checkpoint()
                    if time.monotonic()-waiting>21600:raise TimeoutError('host quiet admission exceeded six hours')
            folder=output/name;folder.mkdir();container='sp11-'+str(os.getpid())+'-'+str(index)
            command=['docker','run','--rm','--init','--name',container,'--network','none',
                '--cpuset-cpus',','.join(map(str,affinity(machine,step['cpus']))),
                '--memory',str(config['memory_gib'])+'g','--memory-swap',str(config['memory_gib'])+'g',
                '--user',f'{os.getuid()}:{os.getgid()}',
                '-v',str(ROOT)+':/repo:ro','-v',config['runtime']['release']+':/release:ro',
                '-v',str(folder)+':/reports','-w','/repo',config['image'],'python3','install/native/catalog_gate.py',
                '--timeout',str(step['seconds']+600),'--report','/reports/cleanup.json','--','python3',
                'e2e/native/performance/sp11_trial.py','--release','/release','--scratch','/reports',
                '--report','/reports/result.json','--seconds',str(step['seconds']),
                '--rate',str(config['rate']),'--clients',str(config['clients']),'--warmup','60']
            started=time.time()*1000;checkpoint(status='running',container=container)
            with (output/(name+'.log')).open('x') as log:
                child=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
                checkpoint(child_pid=child.pid);completed=False
                try:
                    while True:
                        try:code=child.wait(timeout=10);completed=True;break
                        except subprocess.TimeoutExpired:checkpoint();monitor.check()
                finally:
                    stop_interrupted(child,container,completed)
                    ended=time.time()*1000
                    receipt=dict(step=step,quiet=quiet,started_at_ms=started,ended_at_ms=ended,
                        overlaps=monitor.overlap(started,ended),
                        sha256={p.name:sha(p) for p in folder.iterdir() if p.is_file()})
                    save(folder/'receipt.json',receipt);checkpoint(child_pid=None)
            verify()
            if code:raise RuntimeError('fixture or window gate failed: '+name+' (exit '+str(code)+')')
            if receipt['overlaps']:raise RuntimeError('host contention: preserve trial for review')
            report=json.loads((folder/'result.json').read_text());cleanup=json.loads((folder/'cleanup.json').read_text())
            windows=json.loads((folder/'windows.json').read_text())
            if windows['status']!='passed' or not all(windows['gates'].values()):raise ValueError('window gate failed')
            if report['status']!='measured' or not report['within_5s_p95'] or not report['offered_load_met']:raise ValueError('whole-run gate failed')
            if report['release_identity']!=config['runtime']['identity']['release_identity']:raise ValueError('trial package differs')
            if cleanup['exit_code'] or cleanup['leaked_descendants'] or cleanup['remaining_descendants'] or cleanup['timed_out']:raise ValueError('cleanup failed')
            if not json.loads((folder/'reopen.json').read_text()):raise ValueError('reopen evidence missing')
            if json.loads((folder/'history.json').read_text())['foreign_keys']!='passed':raise ValueError('history invalid')
            state['completed'].append(name);checkpoint(status='between_trials')
        checkpoint(status='steady_measurements_complete_review_required',phase='review')
    except BaseException as error:
        checkpoint(status='stopped_for_investigation',error_type=type(error).__name__,error=str(error));raise
    finally:
        try:monitor.close()
        except BaseException as error:
            checkpoint(status='stopped_for_investigation',error='host monitor close failed: '+type(error).__name__);raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    def interrupted(signum,frame):raise KeyboardInterrupt('controller interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    a=p.parse_args();run(a.config.resolve(),a.output.resolve())
