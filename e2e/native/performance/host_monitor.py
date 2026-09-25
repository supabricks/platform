"""Bounded, safe same-host observations; no unrelated process is modified."""
import errno
import json
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
                 total_bytes=256*1024**2):
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
        self.part = len(list(self.directory.glob('*.jsonl')))
        self.written = sum(p.stat().st_size for p in self.directory.glob('*.jsonl'))
        self.segment_written = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()
        return self

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
                    self.previous = sample['builds']
                    self.last_sample = sample['monotonic']
                    self.samples += 1
                    if sample['active_builds']:
                        self.last_active = sample['monotonic']
                        self.events.append(sample['at_ms'])
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
                return dict(quiet_seconds=quiet, sample_count=self.samples, at_ms=time.time()*1000)
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
