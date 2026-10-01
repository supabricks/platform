#!/usr/bin/env python3
"""Continuous lifecycle gate plus idle CPU and control responsiveness observations."""
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuous


def cpu_usage():
    values = dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())
    return int(values['usage_usec']) / 1e6


class DispatchProbe(continuous.Continuous):
    def workload(self, cap):
        # The inherited run has finished bootstrap and proved healthy idle state.
        epoch = self.current()['epoch_id']
        before = cpu_usage()
        start = time.monotonic()
        time.sleep(20)
        elapsed = time.monotonic() - start
        idle_cores = (cpu_usage() - before) / elapsed
        assert self.current()['epoch_id'] == epoch, 'idle publication churn'
        latency = []
        for _ in range(50):
            start = time.perf_counter()
            self.request(method='status')
            latency.append((time.perf_counter()-start)*1000)
            time.sleep(.1)
        assert self.current()['epoch_id'] == epoch, 'status probes create epochs'
        self.metrics['dispatch_component'] = dict(
            scope='One fresh fixture; idle cgroup CPU includes all services; API samples are correlated',
            script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            idle_seconds=elapsed, idle_cpu_cores=idle_cores,
            status_latency_ms=continuous.percentiles(latency),
            status_samples_ms=latency, idle_epoch_unchanged=True)
        self.check('idle_cpu_and_status_latency_recorded_without_empty_publications')
        super().workload(cap)


if __name__ == '__main__':
    continuous.Continuous = DispatchProbe
    continuous.main()
