"""Linux-only sampled worker RSS and owned spill metrics for qualification."""
from pathlib import Path
import threading


class SessionMetrics:
    def __init__(self, workspace):
        self.workspace = workspace
        target = str(workspace / 'input.json').encode()
        matches = []
        for proc in Path('/proc').glob('[0-9]*'):
            try:
                if target in (proc / 'cmdline').read_bytes().split(b'\0'):
                    matches.append(proc)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        if len(matches) != 1:
            raise ValueError('expected one owned analytical worker for resource sampling')
        self.proc = matches[0]
        self.result = dict(interval_ms=100, samples=0, peak_rss_bytes=0,
                           peak_spill_bytes=0, peak_spill_file_bytes=0)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.done.is_set():
            try:
                for line in (self.proc / 'status').read_text().splitlines():
                    if line.startswith('VmRSS:'):
                        self.result['peak_rss_bytes'] = max(self.result['peak_rss_bytes'], int(line.split()[1]) * 1024)
                sizes = []
                for path in (self.workspace / 'spill').rglob('*'):
                    try:
                        if path.is_file(): sizes.append(path.stat().st_size)
                    except FileNotFoundError:
                        pass
                self.result['peak_spill_bytes'] = max(self.result['peak_spill_bytes'], sum(sizes))
                self.result['peak_spill_file_bytes'] = max([self.result['peak_spill_file_bytes']] + sizes)
                self.result['samples'] += 1
            except (FileNotFoundError, ProcessLookupError):
                break
            self.done.wait(.1)

    def finish(self):
        self.done.set()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive(), 'resource sampler did not stop'
        assert self.result['samples'] > 0, 'no worker resource samples'
        return self.result
