#!/usr/bin/env python3
"""SP10 sequential frozen campaign; execute under a supervised user service.

A ten-second heartbeat reports liveness. Errors stop; completed evidence is never
reset. Only bounded whole-pair build-contention replacement is automatic.
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
from compare import ROOT, harness_identity, package_identity
from diagnostic_preflight import check as diagnostic_preflight
from host_monitor import HostMonitor
from matrix import affinity, topology


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())
def save(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def run(config_path, root):
    root.mkdir(parents=True,exist_ok=False)
    lock=(root/'.campaign.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    config=read(config_path)
    state=dict(status='preparing',pid=os.getpid(),config=config,config_sha256=sha(config_path),
        controller_identity=harness_identity(ROOT),driver_sha256=sha(Path(__file__)),completed=[])
    def checkpoint(**values):
        state.update(values,heartbeat_ms=time.time()*1000);save(root/'status.json',state)
    def verify():
        assert sha(config_path)==state['config_sha256']
        assert harness_identity(ROOT)==state['controller_identity']
        assert shutil.disk_usage(root).free>=64*1024**3
        for arm in config['arms'].values():assert package_identity(Path(arm['release']),arm['revision'])==arm['identity']
    def execute(name,command,phase=None):
        verify();checkpoint(status='running',phase=phase or name)
        with (root/(name+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT)
            checkpoint(child_pid=child.pid)
            while True:
                try:code=child.wait(timeout=10);break
                except subprocess.TimeoutExpired:checkpoint()
        checkpoint(child_pid=None);return code
    def quiet(monitor):
        checkpoint(status='waiting_for_quiet')
        # HostMonitor blocks until quiet; keep the heartbeat alive while waiting.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(monitor.wait_quiet)
            while True:
                try:return future.result(timeout=10)
                except concurrent.futures.TimeoutError:checkpoint()
    def fixtures(name,plans):
        folder=root/name;folder.mkdir();monitor=HostMonitor(folder).start();receipts=[]
        try:
            for index,plan in enumerate(plans):
                for attempt in range(1,4):
                    pair=[]
                    for step in plan:
                        admitted=quiet(monitor);key=f'{index+1:02}-attempt{attempt:02}-'+step['label']
                        output=folder/key;output.mkdir()
                        arm=config['arms'][step['arm']]
                        command=['docker','run','--rm','--init','--name','sp10a-'+str(os.getpid()),
                            '--network','none','--cpuset-cpus',','.join(map(str,affinity(topology(),step['cpus']))),
                            '--memory','16g','--memory-swap','16g','--user',f'{os.getuid()}:{os.getgid()}',
                            '-v',str(ROOT)+':/repo:ro','-v',arm['release']+':/release:ro',
                            '-v',str(output)+':/reports','-w','/repo',config['image'],
                            'python3','install/native/catalog_gate.py','--timeout',str(step['timeout']),
                            '--report','/reports/cleanup.json','--',*step['command']]
                        started=time.time()*1000;code=execute(name+'-'+key,command,name);ended=time.time()*1000
                        receipt=dict(step=step,directory=key,started_at_ms=started,ended_at_ms=ended,
                            exit_code=code,quiet=admitted,overlaps=monitor.overlap(started,ended),
                            sha256={p.name:sha(p) for p in output.glob('*.json')})
                        receipts.append(receipt);pair.append(receipt);save(folder/'receipts.json',receipts)
                        assert code==0,'fixture failed: '+name+'/'+key
                        cleanup=read(output/'cleanup.json');assert cleanup['exit_code']==cleanup['leaked_descendants']==cleanup['remaining_descendants']==0 and not cleanup['timed_out']
                        report=read(output/'result.json');assert report['status'] in ('PASS','measured')
                        if 'observer' in report:
                            assert report['offered_load_met'],'observer control source/input failure; retain and investigate'
                            assert report['release_identity']==arm['identity']['release_identity']
                            assert 'both_published_tables_equal_frozen_postgres_source' in report['checks']
                    accepted=not any(r['overlaps'] for r in pair)
                    for receipt in pair:receipt['accepted']=accepted
                    save(folder/'receipts.json',receipts)
                    if accepted:break
                else:raise RuntimeError('fixture contention replacement limit reached')
        finally:monitor.close()
        state['completed'].append(name);checkpoint(status='between_phases')
    def compare(name,cells,clients,seconds,warmup,activation=False):
        command=[sys.executable,str(ROOT/'e2e/native/performance/compare.py'),
            '--slice',config.get('slice','SP10a')+'-'+name,'--hypothesis',config['hypothesis'],'--output',str(root/name),
            '--cells',cells,'--clients',str(clients),'--seconds',str(seconds),'--warmup-seconds',str(warmup),
            '--repeats','3','--quiet-seconds','300','--minimum-free-gib','64','--image',config['image']]
        for label,key in [('predecessor','candidate' if activation else 'predecessor'),('candidate','candidate')]:
            arm=config['arms'][key]
            command+=['--'+label+'-release',arm['release'],'--'+label+'-revision',arm['revision'],
                      '--'+label+'-harness',config['workload_harness']]
        if activation:command+=['--activation-control']
        code=execute(name,command)
        if (root/name/'experiment.json').exists():
            archive=[sys.executable,str(ROOT/'e2e/native/performance/archive_comparison.py'),str(root/name),str(root/'archives'/name)]
            subprocess.run(archive+(['--allow-incomplete'] if code else []),check=True)
        assert code==0,'comparison failed: '+name
        assert read(root/name/'experiment.json')['state']=='complete'
        state['completed'].append(name);checkpoint(status='between_phases')
    def paired_components(kind):
        plans=[]
        for repeat in range(3):
            plan=[]
            for arm in (['predecessor','candidate'] if repeat%2==0 else ['candidate','predecessor']):
                if kind=='lifecycle':
                    command=['python3','e2e/native/performance/reuse_component.py','--binary','/release/bin/supabricks',
                        '--bundle','/release/engine','--helpers','/release/helpers','--python','/release/python/analytics/python',
                        '--worker','/release/python/analytics/export.py','--report','/reports/result.json']
                else:command=['/release/python/analytics/python','e2e/native/performance/'+('owner_component.py' if config.get('journal_component')=='owner' else 'backend_component.py'),
                    '--analytics','/release/python/analytics','--scratch','/reports','--report','/reports/result.json']
                plan.append(dict(label=arm,arm=arm,cpus=8,timeout=900,command=command))
            plans.append(plan)
        fixtures(kind,plans)
    def observer_controls(arm):
        plans=[]
        for cpu in (8,16):
            for repeat in range(3):
                plan=[]
                for enabled in (['off','on'] if repeat%2==0 else ['on','off']):
                    command=['python3','e2e/native/performance/backend_observer_control.py','--release','/release',
                        '--scratch','/reports','--report','/reports/result.json','--observer',enabled]
                    plan.append(dict(label=enabled,arm=arm,cpus=cpu,timeout=900,command=command))
                plans.append(plan)
        fixtures(arm+'-observer-controls',plans)
    try:
        checkpoint();verify()
        save(root/'diagnostic-preflight.json',{key:diagnostic_preflight(Path(arm['release'])/'bin/supabricks') for key,arm in config['arms'].items()})
        installations={key:json.loads(subprocess.check_output([str(Path(arm['release'])/'bin/supabricks'),'installation','verify'],text=True)) for key,arm in config['arms'].items()}
        assert all(v['verified'] and v['identity']==config['arms'][k]['identity']['release_identity'] for k,v in installations.items())
        save(root/'installation-preflight.json',installations)
        paired_components('backend-component');paired_components('lifecycle')
        observer_controls('predecessor')
        compare('historical-main','4:50,16:50,8:1000,16:1000',4,45,5)
        compare('qualified-main','8:1250,16:1250',8,300,60)
        compare('historical-profiler-controls','4:50,16:50,8:1000,16:1000',4,45,5,True)
        compare('qualified-profiler-controls','8:1250,16:1250',8,300,60,True)
        observer_controls('candidate')
        checkpoint(status='measurements_complete_review_required',phase='review',completed_at_ms=time.time()*1000)
    except BaseException as error:
        checkpoint(status='stopped_for_investigation',error_type=type(error).__name__,error=str(error));raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.config.resolve(),a.output.resolve())
