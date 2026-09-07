#!/usr/bin/env python3
"""Qualification-only sampler: installation daemon and all live descendants."""
import argparse
import json
import os
from pathlib import Path
import platform
import time
import psutil


def sample(args):
    peak_rss = 0
    cpu_seconds = 0
    observed = {}
    samples = 0
    start = time.monotonic()
    while not args.stop.exists():
        processes = {}
        for p in psutil.process_iter(['pid', 'cmdline']):
            cmd = p.info['cmdline'] or []
            if 'daemon' in cmd and str(args.data) in cmd:
                try:
                    for child in [p, *p.children(recursive=True)]:
                        processes[child.pid] = child
                except psutil.Error:
                    pass
        rss = 0
        for p in processes.values():
            try:
                rss += p.memory_info().rss
                cpu = p.cpu_times()
                key = (p.pid, p.create_time())
                total = cpu.user + cpu.system
                if key in observed:
                    cpu_seconds += max(0, total - observed[key])
                observed[key] = total
            except psutil.Error:
                pass
        peak_rss = max(peak_rss, rss)
        samples += 1
        time.sleep(0.2)
    elapsed = time.monotonic() - start
    args.report.write_text(json.dumps(dict(host=platform.platform(), machine=platform.machine(),
        logical_cpus=os.cpu_count(), host_memory_bytes=psutil.virtual_memory().total,
        duration_seconds=elapsed, peak_rss_bytes=peak_rss, cpu_seconds=cpu_seconds,
        mean_cpu_percent_one_core=100 * cpu_seconds / elapsed, samples=samples,
        scope='daemon and sampled live descendants, including storage, computes and analytics; excludes harness and CLI; RSS can double-count shared pages and miss short peaks'), indent=2) + '\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--stop', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    sample(p.parse_args())
