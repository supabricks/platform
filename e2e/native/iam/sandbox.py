"""Host orchestrator: no Docker daemon config changes; only labeled throwaway containers."""
import concurrent.futures
import json
import subprocess
import socket
import psutil
from common import HERE, PINS, command, wait


def prepare_data(release, root):
    code = '''import os,sys\nfrom pathlib import Path\nimport numpy as np\nimport pyarrow as pa\nfrom deltalake import write_deltalake, WriterProperties\nroot=Path(sys.argv[1])\nfor who in ('alice','bob'):\n p=root/who; p.mkdir(); (p/'owner').write_text(who)\n for mb in (10,100):\n  n=mb*1024\n  table=pa.table({'id':np.arange(n,dtype=np.int64),'payload':[os.urandom(1024) for _ in range(n)]})\n  write_deltalake(p/f'data{mb}',table,writer_properties=WriterProperties(compression='UNCOMPRESSED'))\n'''
    command(release/'python/analytics/python','-c',code,root,timeout=120)
    # Fixture data is deliberately readable by sandbox UID 1000 even when the
    # host runner has a different UID. Only the selected tenant tree is mounted.
    for path in root.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)


def probe(release, tools, root, evidence, forbidden_tcp):
    data=root/'datasets'; data.mkdir()
    prepare_data(release,data)
    image=PINS['rootfs']
    rootfs_name='iam00-rootfs-'+root.name
    command('docker','create','--name',rootfs_name,image)
    try:
        command('docker','export','-o',root/'rootfs.tar',rootfs_name)
    finally:
        command('docker','rm','-f',rootfs_name)
    names=[]; roots=[]; canaries=[]; owned_processes=set()
    try:
        for who,mode in [('alice','revoke'),('bob','daemon-loss')]:
            work=root/who; work.mkdir(); roots.append(work)
            config=root/(who+'.json')
            config.write_text(json.dumps(dict(principal=who,revocation=mode,
                forbidden_paths=['/host-secrets/credential','/host-secrets/control.sock',
                    '/host-secrets/other/private-cache','/host-secrets/other/private-credential',
                    '/other-data/owner','/other-data/data100/_delta_log/00000000000000000000.json',
                    '/work/config.json','/proc/1/root/host-secrets/credential'],
                forbidden_tcp=forbidden_tcp+[['172.17.0.1',5432],['1.1.1.1',443]])))
            config.chmod(0o644)
            secrets=root/(who+'-host-secrets'); secrets.mkdir()
            (secrets/'credential').write_text('IAM00 private host credential')
            canary=socket.socket(socket.AF_UNIX); canary.bind(str(secrets/'control.sock')); canary.listen()
            canaries.append(canary)
            other=secrets/'other'; other.mkdir()
            for file in ('private-cache','private-credential'): (other/file).write_text('other principal private data')
            name='iam00-'+who+'-'+root.name; names.append(name)
            args=['docker','run','-d','--name',name,'--label','supabricks.iam00=true',
                '--privileged','--network','none','--memory=2g','--memory-swap=2g','--cpus=2','--pids-limit=512',
                '-v',f'{tools}:/tools:ro','-v',f'{release}:/product:ro','-v',f'{HERE}:/probe:ro',
                '-v',f'{work}:/work','-v',f'{config}:/probe-config.json:ro',
                '-v',f'{root}/rootfs.tar:/rootfs.tar:ro','-v',f'{data/who}:/admitted:ro',
                '-v',f'{secrets}:/host-secrets:ro','-v',f'{data/("bob" if who=="alice" else "alice")}:/other-data:ro',
                '-w','/work',image,'/product/python/runtime/bin/python3.12','-I','-B','/probe/controller.py']
            command(*args)
        def ready():
            for name in names:
                state=json.loads(command('docker','inspect',name))[0]['State']
                if not state['Running']:
                    raise RuntimeError('sandbox controller stopped; inspect private container logs')
            return all((work/'ready').exists() for work in roots)
        wait(ready,240)
        evidence.check('two_concurrent_managed_notebooks_and_sail_instances',True)
        for name in names:
            pid=json.loads(command('docker','inspect',name))[0]['State']['Pid']
            parent=psutil.Process(pid)
            owned_processes.add(parent); owned_processes.update(parent.children(recursive=True))
        for work in roots: (work/'revoke').write_text('requested')
        def stopped(name):
            return command('docker','wait',name,timeout=45)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(stopped,names))
        evidence.check('both_sandbox_controllers_exit_successfully',results==['0','0'])
        for work in roots:
            result=json.loads((work/'result.json').read_text())
            for check in result.pop('checks'):
                evidence.check(work.name+':'+check,True)
            evidence.check(work.name+':bounded_execution_revocation',result['revocation']['seconds']<=60
                and result['revocation']['heartbeat_stopped'] and result['revocation']['runtime_empty'])
            evidence.check(work.name+':outer_cgroup_limits',result['outer_cgroup_memory_max']=='2147483648'
                and result['outer_cgroup_cpu_max']=='200000 100000' and result['outer_cgroup_pids_max']=='512')
            evidence.metrics[work.name]=result
    finally:
        for canary in canaries: canary.close()
        for name in names:
            with (root/(name+'.log')).open('w') as log:
                subprocess.run(['docker','logs',name],stdout=log,stderr=subprocess.STDOUT,timeout=15)
            command('docker','rm','-f',name)
        running=command('docker','ps','-aq','--filter','label=supabricks.iam00=true').splitlines()
        evidence.check('owned_sandbox_containers_removed',not(set(names)&set(running)))
        for process in owned_processes:
            try: assert not process.is_running() or process.status()==psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess: pass
        evidence.check('owned_sandbox_host_processes_reaped',True)
