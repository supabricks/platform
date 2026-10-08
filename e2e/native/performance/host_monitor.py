"""Bounded, safe same-host observations; no unrelated process is modified."""
import errno
import json
import hashlib
import math
import os
from pathlib import Path
import shutil
import threading
import time

BUILD_NAMES = {'cargo', 'cargo-mutants', 'rustc', 'rustdoc', 'make', 'gmake', 'cmake',
               'ninja', 'go', 'compile', 'gcc', 'g++', 'cc', 'cc1', 'cc1plus',
               'clang', 'clang++', 'ld', 'ld.lld', 'javac', 'gradle', 'mvn', 'npm'}


def active_builds(previous, current):
    # A parked build parent does not make the host busy forever. New identities
    # and changing CPU/I/O counters do; PID reuse cannot inherit idle status.
    return [key for key, value in current.items() if value.get('io_error') or previous.get(key) != value]


def build_observation(path, name, fields):
    try:
        io = {k: int(v) for k, v in (line.split(': ') for line in (path / 'io').read_text().splitlines())}
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EPERM):
            raise
        # A process can change access during exec. Unknown activity is never
        # idle: retain it and block quiet admission until readable or gone.
        return dict(tool=name if name in BUILD_NAMES else 'build-child',
                    user_ticks=int(fields[11]), system_ticks=int(fields[12]),
                    io=None, io_error='permission_denied')
    return dict(tool=name if name in BUILD_NAMES else 'build-child',
                user_ticks=int(fields[11]), system_ticks=int(fields[12]), io=io)


def observe(root, previous):
    builds = {}
    processes = {}
    for path in Path('/proc').glob('[0-9]*'):
        try:
            name = (path / 'comm').read_text().strip()
            fields = (path / 'stat').read_text().rsplit(') ', 1)[1].split()
            processes[int(path.name)] = (path, name, fields)
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ESRCH):
                raise
    selected = {pid for pid, (_, name, _) in processes.items() if name in BUILD_NAMES}
    # Include test/linker children of parked build parents, without storing argv.
    while True:
        children = {pid for pid, (_, _, fields) in processes.items() if int(fields[1]) in selected}
        expanded = selected | children
        if expanded == selected:
            break
        selected = expanded
    for pid in selected:
        path, name, fields = processes[pid]
        try:
            identity = f'{pid}:{fields[19]}'
            builds[identity] = build_observation(path, name, fields)
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ESRCH):
                raise
    sensors = {}
    for path in [*Path('/sys/class/thermal').glob('thermal_zone*/temp'),
                 *Path('/sys/devices/system/cpu').glob('cpu*/cpufreq/scaling_cur_freq')]:
        try:
            sensors[str(path.relative_to('/sys'))] = int(path.read_text())
        except (OSError, ValueError):
            pass  # Optional sensors; absence remains visible through coverage.
    return dict(at_ms=time.time()*1000, monotonic=time.monotonic(), builds=builds,
                active_builds=active_builds(previous, builds),
                cpu=Path('/proc/stat').read_text().splitlines()[0],
                pressure={n: Path('/proc/pressure', n).read_text() for n in ('cpu', 'io', 'memory')},
                memory=Path('/proc/meminfo').read_text(), load=Path('/proc/loadavg').read_text(),
                diskstats=Path('/proc/diskstats').read_text(),
                free_bytes=shutil.disk_usage(root).free, sensors=sensors)


class HostMonitor:
    """Five-second JSONL samples, bounded rotation and a fail-closed monitor."""
    def __init__(self, root, quiet_seconds=300, interval=5, segment_bytes=16*1024**2,
                 total_bytes=256*1024**2, continuity=None, publish_quiet=False):
        self.root = Path(root)
        self.directory = self.root / 'host'
        self.directory.mkdir(exist_ok=True)
        self.quiet_seconds, self.interval = quiet_seconds, interval
        self.segment_bytes, self.total_bytes = segment_bytes, total_bytes
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.error = None
        self.last_active = time.monotonic()
        self.last_sample = self.last_active
        self.previous = {}
        self.samples = 0
        self.events = []
        self.continuity = Path(continuity) if continuity else None
        self.publish_quiet = publish_quiet
        self.quiet_path = self.directory / 'quiet-state.json'
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip() if continuity or publish_quiet else None
        self.inherited = None
        self.part = len(list(self.directory.glob('*.jsonl')))
        self.written = sum(p.stat().st_size for p in self.directory.glob('*.jsonl'))
        self.segment_written = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.inherit_quiet()
        self.thread.start()
        return self

    def inherit_quiet(self):
        """Carry fresh evidence from the continuously running campaign monitor.

        A stale/missing checkpoint earns no quiet credit. Local sampling still
        runs throughout the phase and detects activity after the handoff.
        """
        if self.continuity is None:return
        evidence = dict(source=str(self.continuity), accepted=False)
        try:
            with self.continuity.open('rb') as source:data=source.read(1024*1024+1)
            if len(data)>1024*1024:raise ValueError('checkpoint budget')
            value=json.loads(data);now=time.monotonic()
            active,sampled=value['last_active'],value['last_sample']
            if (value['version']!=1 or value['boot_id']!=self.boot_id
                or not all(type(v) in (int,float) and math.isfinite(v) for v in (active,sampled))
                or not 0<=active<=sampled<=now or now-sampled>max(20,self.interval*4)
                or type(value['samples']) is not int or value['samples']<1
                or not isinstance(value['previous'],dict)):
                raise ValueError('checkpoint not continuous')
            self.last_active=active;self.previous=value['previous']
            self.inherited=dict(source=str(self.continuity),sha256=hashlib.sha256(data).hexdigest())
            evidence.update(accepted=True,sha256=self.inherited['sha256'],checkpoint=value)
        except (OSError,ValueError,KeyError,TypeError):
            evidence['reason']='missing_invalid_or_stale_checkpoint; fresh quiet interval required'
        (self.directory/'quiet-continuity.json').write_text(json.dumps(evidence,indent=2)+'\n')

    def check(self):
        if self.error:
            raise RuntimeError('host monitor failed: ' + self.error)
        if time.monotonic() - self.last_sample > max(20, self.interval * 4):
            raise RuntimeError('host monitor sampling gap')

    def run(self):
        try:
            while not self.stop.is_set():
                sample = observe(self.root, self.previous)
                encoded = (json.dumps(sample, separators=(',', ':')) + '\n').encode()
                with self.lock:
                    if self.written + len(encoded) > self.total_bytes:
                        raise RuntimeError('host evidence budget exhausted')
                    if self.segment_written + len(encoded) > self.segment_bytes:
                        self.part += 1
                        self.segment_written = 0
                    with (self.directory / f'{self.part:04}.jsonl').open('ab') as stream:
                        stream.write(encoded)
                    self.written += len(encoded)
                    self.segment_written += len(encoded)
                    if sample['monotonic']-self.last_sample>max(20,self.interval*4):
                        # Resumed sampling cannot certify the unobserved gap.
                        self.last_active=sample['monotonic']
                    self.previous = sample['builds']
                    self.last_sample = sample['monotonic']
                    self.samples += 1
                    if sample['active_builds']:
                        self.last_active = sample['monotonic']
                        self.events.append(sample['at_ms'])
                    if self.publish_quiet:
                        checkpoint=dict(version=1,boot_id=self.boot_id,last_active=self.last_active,
                            last_sample=self.last_sample,samples=self.samples,previous=self.previous)
                        data=json.dumps(checkpoint).encode()
                        if len(data)>1024*1024:raise RuntimeError('quiet checkpoint budget exhausted')
                        temporary=self.quiet_path.with_suffix('.tmp')
                        temporary.write_bytes(data);temporary.replace(self.quiet_path)
                self.stop.wait(self.interval)
        except Exception as error:
            self.error = type(error).__name__ + ': ' + str(error)

    def wait_quiet(self, max_wait=21600):
        start = time.monotonic()
        while True:
            self.check()
            with self.lock:
                quiet = time.monotonic() - self.last_active
                sampled = self.samples > 0
            if sampled and quiet >= self.quiet_seconds:
                receipt=dict(quiet_seconds=quiet, sample_count=self.samples, at_ms=time.time()*1000)
                if self.inherited is not None:receipt['continuity']=self.inherited
                return receipt
            if time.monotonic() - start >= max_wait:
                raise TimeoutError('host did not reach the declared quiet interval')
            self.stop.wait(min(self.interval, max_wait))

    def overlap(self, start_ms, end_ms):
        self.check()
        with self.lock:
            return [t for t in self.events if start_ms <= t <= end_ms]

    def close(self):
        self.stop.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError('host monitor did not stop')
        if self.error:
            raise RuntimeError('host monitor failed: ' + self.error)
