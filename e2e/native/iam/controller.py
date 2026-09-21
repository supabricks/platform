"""Trusted disposable outer container; workload executes only through runsc.

Outer cgroup provides the measured budget; inner runsc must not use the outer
container's read-only cgroup mount. This is a feasibility topology, not an installer.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import time

ROOT = Path('/work')
RUNTIME = ['/tools/runsc', '--root=/work/runsc', '--network=none', '--platform=systrap',
           '--ignore-cgroups', '--sidecar-usage-policy=STRICT']


def run(*args):
    return subprocess.run([*RUNTIME,*args], check=True, capture_output=True, text=True, timeout=20)


def main():
    rootfs = ROOT/'rootfs'; rootfs.mkdir()
    subprocess.run(['tar','-xf','/rootfs.tar','-C',str(rootfs)],check=True)
    (rootfs/'etc/timezone').write_text('Etc/UTC\n')
    shutil.copyfile('/product/python/runtime/lib/python3.12/site-packages/tzdata/zoneinfo/UTC',rootfs/'etc/localtime')
    scratch = ROOT/'scratch'; scratch.mkdir()
    subprocess.run(['mount','-t','tmpfs','-o','size=512m,uid=1000,gid=1000,mode=0700,nosuid,nodev',
        'iam00-scratch',str(scratch)],check=True)
    config = json.loads(Path('/probe-config.json').read_text())
    run('spec','--','/product/python/runtime/bin/python3.12','-I','-B','/probe/workload.py')
    path = ROOT/'config.json'; spec = json.loads(path.read_text())
    spec['process'].update(user=dict(uid=1000,gid=1000),cwd='/scratch',noNewPrivileges=True,
        capabilities={k:[] for k in ('bounding','effective','inheritable','permitted','ambient')},
        env=['PATH=/usr/bin:/bin','HOME=/scratch','LANG=C.UTF-8','OPENBLAS_NUM_THREADS=1',
             'OMP_NUM_THREADS=1','TZ=UTC','RAYON_NUM_THREADS=2','TOKIO_WORKER_THREADS=2','PYTHONDONTWRITEBYTECODE=1'])
    for source,destination in [('/product','/product'),('/probe','/probe'),('/admitted','/admitted'),('/probe-config.json','/probe-config.json')]:
        spec['mounts'].append(dict(destination=destination,source=source,type='bind',options=['bind','ro','nosuid','nodev']))
    spec['mounts'].append(dict(destination='/scratch',source=str(scratch),type='bind',options=['bind','rw','nosuid','nodev']))
    spec['mounts'].append(dict(destination='/tmp',source='tmpfs',type='tmpfs',options=['nosuid','nodev','size=67108864']))
    spec['mounts'].append(dict(destination='/dev/shm',source='tmpfs',type='tmpfs',options=['nosuid','nodev','size=67108864']))
    path.write_text(json.dumps(spec))
    # Positive host canaries exist outside the sandbox but within this disposable controller.
    canary = subprocess.Popen(['/product/python/runtime/bin/python3.12','/probe/lease_issuer.py','IAM00_HOST_CANARY'],
        env=dict(os.environ,IAM00_HOST_SECRET='IAM00_HOST_CANARY'))
    process = None
    try:
        with (ROOT/'runsc.log').open('w') as log:
            process = subprocess.Popen([*RUNTIME,'run','lease'],stdout=log,stderr=subprocess.STDOUT)
        deadline = time.monotonic()+180
        while not (scratch/'lease-ready').exists():
            if process.poll() is not None: raise RuntimeError('sandbox exited before ready')
            if time.monotonic()>deadline: raise TimeoutError('sandbox readiness')
            time.sleep(.1)
        result = json.loads((scratch/'result.json').read_text())
        result['oci_config_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        result['scratch_capacity_bytes']=os.statvfs(scratch).f_blocks*os.statvfs(scratch).f_frsize
        result['outer_cgroup_memory_max']=Path('/sys/fs/cgroup/memory.max').read_text().strip()
        result['outer_cgroup_cpu_max']=Path('/sys/fs/cgroup/cpu.max').read_text().strip()
        result['outer_cgroup_pids_max']=Path('/sys/fs/cgroup/pids.max').read_text().strip()
        result['outer_memory_current_bytes']=int(Path('/sys/fs/cgroup/memory.current').read_text())
        (ROOT/'ready').write_text('ready')
        # Host orchestrator waits until both tenants are concurrently busy, then requests revoke.
        deadline=time.monotonic()+120
        while not (ROOT/'revoke').exists():
            if time.monotonic()>deadline: raise TimeoutError('revoke request')
            time.sleep(.05)
        mode=config['revocation']
        start=time.monotonic()
        first=(scratch/'open-file-heartbeat').read_text()
        # The issuer renews a two-second monotonic lease outside the workload.
        # After issuer death the independent watchdog receives no more renewals.
        if mode=='daemon-loss':
            canary.kill(); canary.wait(timeout=5)
            while time.monotonic() < float((ROOT/'renewal').read_text()):
                time.sleep(.02)
        run('kill','--all','lease','KILL')
        process.wait(timeout=10)
        run('delete','--force','lease')
        elapsed=time.monotonic()-start
        final=(scratch/'open-file-heartbeat').read_text()
        time.sleep(.3)
        assert (scratch/'open-file-heartbeat').read_text()==final
        remaining=json.loads(run('list','--format=json').stdout)
        assert remaining in (None, []), 'gVisor still owns a sandbox'
        result['revocation']=dict(mode=mode,seconds=round(elapsed,3),
            open_descriptor_activity_before=bool(first), heartbeat_stopped=True, runtime_empty=True,
            lease_seconds=2, issuer_killed=mode=='daemon-loss')
        result['outer_memory_peak_bytes']=int(Path('/sys/fs/cgroup/memory.peak').read_text())
        (ROOT/'result.json').write_text(json.dumps(result,indent=2))
    finally:
        if process is not None:
            subprocess.run([*RUNTIME,'kill','--all','lease','KILL'],capture_output=True,timeout=20)
            process.wait(timeout=15)
            subprocess.run([*RUNTIME,'delete','--force','lease'],capture_output=True,timeout=20)
        if canary.poll() is None: canary.kill()
        canary.wait(timeout=5)
        diagnostics=ROOT/'private-diagnostics'; diagnostics.mkdir(exist_ok=True)
        for name in ('jupyter.log','sail.log','kernel-error.json','result.json'):
            source=scratch/name
            if source.is_file():
                with source.open('rb') as stream: (diagnostics/name).write_bytes(stream.read(262144))
        subprocess.run(['umount',str(scratch)],check=True,timeout=10)


if __name__=='__main__': main()
