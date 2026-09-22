"""Trusted per-lease launcher. Untrusted code runs only in nested gVisor.

No user-selected command, mount, environment, runtime flag or host socket.
Docker owns the outer cgroup; runsc ignores only its read-only nested interface.
The monotonic renewal pipe belongs to the control plane, outside the sandbox.
"""
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path('/work')
RUNTIME = ['/tools/runsc', '--root=/work/runsc', '--network=none',
           '--platform=systrap', '--ignore-cgroups', '--sidecar-usage-policy=STRICT']
LIMIT = 32 * 1024


def call(*args, check=True):
    return subprocess.run([*RUNTIME, *args], cwd=ROOT, check=check,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)


def diagnostic():
    with (ROOT/'runsc.log').open('rb') as stream:
        stream.seek(max(0, os.fstat(stream.fileno()).st_size-16384))
        return stream.read(16384).decode('utf-8', 'replace')


def main():
    # These are the actual enclosing cgroup limits, not requested OCI values.
    assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '2147483648'
    assert Path('/sys/fs/cgroup/memory.swap.max').read_text().strip() == '0'
    assert Path('/sys/fs/cgroup/cpu.max').read_text().strip() == '200000 100000'
    assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '512'
    os.set_blocking(sys.stdin.fileno(), False)
    pending = bytearray()
    deadline = time.monotonic() + 5
    def renew():
        nonlocal deadline, pending
        try:
            value = os.read(sys.stdin.fileno(), 4096)
            if not value:
                raise RuntimeError('renewal channel lost')
            pending.extend(value)
            if len(pending) > 4096:
                raise RuntimeError('renewal limit')
            while b'\n' in pending:
                line, _, pending = pending.partition(b'\n')
                requested = int(line) / 1000
                now = time.monotonic()
                if requested > now + 30.1 or requested <= now:
                    raise RuntimeError('invalid renewal')
                deadline = max(deadline, requested)
        except BlockingIOError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError('lease expired')
    renew()
    rootfs = ROOT / 'rootfs'
    rootfs.mkdir()
    with tarfile.open('/rootfs.tar') as archive:
        # Only operator-pinned rootfs; never an uploaded/executable package.
        def rootfs_filter(member, destination):
            if member.issym() and member.linkname.startswith('/'):
                member = member.replace(linkname=os.path.relpath(member.linkname.lstrip('/'), os.path.dirname(member.name) or '.'))
            return tarfile.data_filter(member, destination)
        archive.extractall(rootfs, filter=rootfs_filter)
    (rootfs / 'etc/timezone').write_text('Etc/UTC\n')
    shutil.copyfile('/product/python/runtime/lib/python3.12/site-packages/tzdata/zoneinfo/UTC', rootfs / 'etc/localtime')
    call('spec', '--', '/product/python/runtime/bin/python3.12', '-I', '-B', '/admission/workload.py')
    path = ROOT / 'config.json'
    spec = json.loads(path.read_text())
    spec['root']['readonly'] = True
    spec['process'].update(user=dict(uid=1000, gid=1000), cwd='/scratch', noNewPrivileges=True,
        capabilities={k: [] for k in ('bounding', 'effective', 'inheritable', 'permitted', 'ambient')},
        rlimits=[dict(type='RLIMIT_NOFILE', hard=256, soft=256), dict(type='RLIMIT_NPROC', hard=256, soft=256)],
        env=['PATH=/usr/bin:/bin', 'HOME=/scratch', 'LANG=C.UTF-8', 'TZ=UTC',
             'OPENBLAS_NUM_THREADS=1', 'OMP_NUM_THREADS=1', 'RAYON_NUM_THREADS=2',
             'TOKIO_WORKER_THREADS=2', 'PYTHONDONTWRITEBYTECODE=1'])
    # Private scratch/cache/output are tmpfs in the sandbox, not shared host paths.
    spec['mounts'] = [m for m in spec['mounts'] if m['destination'] not in ('/tmp', '/dev/shm')]
    for source, target in [('/product', '/product'), ('/admission', '/admission')]:
        spec['mounts'].append(dict(destination=target, source=source, type='bind', options=['rbind', 'ro', 'nosuid', 'nodev']))
    for target, size in [('/scratch', 512*1024*1024), ('/tmp', 64*1024*1024), ('/dev/shm', 64*1024*1024)]:
        spec['mounts'].append(dict(destination=target, source='tmpfs', type='tmpfs',
            options=['nosuid', 'nodev', f'size={size}', 'uid=1000', 'gid=1000', 'mode=0700']))
    path.write_text(json.dumps(spec))
    renew()
    process = None
    output = bytearray()
    try:
        process = subprocess.Popen([*RUNTIME, 'run', 'lease'], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=(ROOT/'runsc.log').open('wb'), close_fds=True)
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(sys.stdin, selectors.EVENT_READ)
        eof = False
        while process.poll() is None or not eof:
            renew()
            for key, _ in selector.select(.05):
                if key.fileobj is process.stdout:
                    block = os.read(process.stdout.fileno(), 65536)
                    if not block:
                        eof = True
                        selector.unregister(process.stdout)
                    output.extend(block)
                    if len(output) > LIMIT:
                        raise RuntimeError('output limit exceeded')
            if eof and process.poll() is not None:
                break
        if process.returncode != 0:
            print(diagnostic(), file=sys.stderr)
        # User output is data. It never controls state, mounts, identity or renewal.
        print(json.dumps(dict(exit_code=process.returncode, output=output.decode('utf-8', 'replace'))), flush=True)
    finally:
        call('kill', '--all', 'lease', 'KILL', check=False)
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        call('delete', '--force', 'lease', check=False)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc(file=sys.stderr)
        if (ROOT/'runsc.log').exists():
            print(diagnostic(), file=sys.stderr)
        # No host configuration, credentials or exception paths in public output.
        print(json.dumps(dict(exit_code=-1, output='', error='sandbox unavailable, expired or resource limit exceeded')), flush=True)
        sys.exit(1)
