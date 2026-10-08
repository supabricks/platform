"""Read-only, bounded host I/O attribution; never stores argv, paths or SQL."""
import errno
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

KNOWN_ROLES = {'postgres', 'safekeeper', 'pageserver', 'supabricks', 'weed', 'cargo', 'rustc'}


def observe(proc=Path('/proc')):
    rows=[];denied=vanished=0
    for path in proc.glob('[0-9]*'):
        try:
            fields=(path/'stat').read_text().rsplit(') ',1)[1].split()
            name=(path/'comm').read_text().strip()
            row=dict(pid=int(path.name),start_ticks=int(fields[19]),ppid=int(fields[1]),
                role=name if name in KNOWN_ROLES else 'other',
                user_ticks=int(fields[11]),system_ticks=int(fields[12]))
            row['cgroup_sha256']=hashlib.sha256((path/'cgroup').read_bytes()).hexdigest()
            try:
                row['io']={k:int(v) for k,v in (line.split(': ') for line in (path/'io').read_text().splitlines())}
            except PermissionError:
                row.update(io=None,io_error='permission_denied');denied+=1
            rows.append(row)
        except OSError as error:
            if error.errno not in (errno.ENOENT,errno.ESRCH):raise
            vanished+=1
    return dict(at_ms=time.time()*1000,processes=rows,permission_denied=denied,vanished=vanished)


def deltas(before,after):
    """Only stable identities/counters qualify; new/exited processes are gaps."""
    key=lambda r:(r['pid'],r['start_ticks'])
    a={key(r):r for r in before['processes']};b={key(r):r for r in after['processes']}
    rows=[];unavailable=[]
    for identity in sorted(a.keys() & b.keys()):
        x,y=a[identity],b[identity]
        if x.get('io') is None or y.get('io') is None:
            unavailable.append(dict(pid=identity[0],start_ticks=identity[1],reason='permission_denied'));continue
        values={k:y['io'][k]-x['io'][k] for k in ('read_bytes','write_bytes')}
        values.update(cpu_ticks=y['user_ticks']+y['system_ticks']-x['user_ticks']-x['system_ticks'])
        if min(values.values())<0 or x['cgroup_sha256']!=y['cgroup_sha256']:
            unavailable.append(dict(pid=identity[0],start_ticks=identity[1],reason='counter_reset_or_group_changed'));continue
        rows.append(dict(pid=identity[0],start_ticks=identity[1],role=y['role'],cgroup_sha256=y['cgroup_sha256'],**values))
    return dict(seconds=(after['at_ms']-before['at_ms'])/1000,processes=rows,
        unavailable=unavailable,new_identities=len(b.keys()-a.keys()),exited_identities=len(a.keys()-b.keys()))


class HostIO:
    def __init__(self,path,interval=1,max_bytes=64*1024**2):
        self.path=Path(path);self.interval=interval;self.max_bytes=max_bytes
        self.container=None;self.owned_group=None;self.binding_attempts=0
        self.rows=[];self.bytes=0;self.elapsed_ns=0;self.error=None
        self.stop=threading.Event();self.last=time.monotonic()
        self.thread=threading.Thread(target=self.run,daemon=True)

    def start(self):self.thread.start();return self

    def bind_container(self,name):
        self.container=name
        if self.owned_group is not None:return
        self.binding_attempts+=1
        result=subprocess.run(['docker','inspect','--format','{{.State.Pid}}',name],
                              capture_output=True,text=True,timeout=2)
        if result.returncode:return  # Matrix may still be starting its container.
        pid=int(result.stdout.strip())
        if pid>0:
            try:self.owned_group=hashlib.sha256(Path(f'/proc/{pid}/cgroup').read_bytes()).hexdigest()
            except FileNotFoundError:pass

    def check(self):
        if self.error:raise RuntimeError('host I/O sampler failed: '+self.error)
        if time.monotonic()-self.last>max(10,self.interval*4):raise RuntimeError('host I/O sampling gap')

    def run(self):
        try:
            while not self.stop.is_set():
                start=time.perf_counter_ns();row=observe();encoded=json.dumps(row,separators=(',',':')).encode()
                if self.bytes+len(encoded)>self.max_bytes:raise RuntimeError('evidence budget exceeded')
                self.bytes+=len(encoded);self.rows.append(row);self.elapsed_ns+=time.perf_counter_ns()-start
                self.last=time.monotonic();self.stop.wait(self.interval)
        except Exception as error:self.error=type(error).__name__+': '+str(error)

    def close(self):
        self.stop.set();self.thread.join(timeout=10)
        if self.thread.is_alive():raise RuntimeError('host I/O sampler did not stop')
        if self.container and self.owned_group is None and not self.error:
            self.error='owned container cgroup was never observed'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        data=dict(scope='SP07 diagnostic observer, not a runtime change. Numeric per-process I/O and CPU; opaque cgroup hashes. Short-lived/inaccessible processes remain coverage gaps. No commands, filenames, row values or credentials.',
            owned_container_cgroup_sha256=self.owned_group,binding_attempts=self.binding_attempts,
            interval_seconds=self.interval,max_uncompressed_sample_bytes=self.max_bytes,
            sample_bytes=self.bytes,monitor_wall_ns=self.elapsed_ns,error=self.error,samples=self.rows)
        self.path.write_bytes(gzip.compress(json.dumps(data,separators=(',',':')).encode(),mtime=0))
        if self.error:raise RuntimeError('host I/O sampler failed: '+self.error)
