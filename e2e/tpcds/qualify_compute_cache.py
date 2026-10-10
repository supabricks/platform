#!/usr/bin/env python3
"""Installed cache profile: default, restart, branch lifecycle and explicit conflicts."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'native')]
from installed_sync import InstalledContinuous
from compute_cache_profile import install
from cell import wait


def run(release, output):
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=False)
    checks = []
    for profile, expected in [('compact', '128MB'), ('source-load', '1GB')]:
        root = output / profile; root.mkdir(mode=0o700)
        cell = InstalledContinuous(release, root)
        # Exercise the unchanged no-flag creation path as the compact control.
        if profile == 'source-load': install(cell, profile)
        try:
            cell.setup_source(str(release / 'python/analytics/python'), release / 'python/analytics/export.py',
                              'CREATE TABLE sample (id int PRIMARY KEY); INSERT INTO sample VALUES (7)')
            def assert_cache(branch):
                wait(lambda: cell.sql(branch, 'SELECT 1') == '1')
                assert cell.sql(branch, 'SHOW shared_buffers') == expected
                assert cell.sql(branch, 'SHOW fsync') == 'on'
                assert cell.sql(branch, 'SHOW full_page_writes') == 'on'
                assert cell.sql(branch, 'SHOW synchronous_commit') == 'on'
                assert cell.sql(branch, 'SHOW wal_level') == 'logical'
            assert_cache(cell.parent)
            other = cell.create('second'); assert_cache(other)
            cell.parent = cell.state(cell.parent, 'suspended')
            cell.parent = cell.state(cell.parent, 'running'); assert_cache(cell.parent)
            before = (root / 'runtime.json').read_bytes()
            wrong = 'compact' if profile == 'source-load' else 'source-load'
            p = subprocess.run([str(cell.binary), 'up', '--data-dir', str(root),
                                '--compute-cache-profile', wrong], capture_output=True)
            assert p.returncode == 4 and b'cache profile differs' in p.stderr + p.stdout
            assert (root / 'runtime.json').read_bytes() == before
            cell.stop()
            # Restart through the normal, no-flag path; no wrapper is involved.
            if 'start' in cell.__dict__: del cell.start
            cell.start(); assert_cache(cell.parent); assert_cache(other)
            assert cell.sql(cell.parent, 'SELECT id FROM sample') == '7'
            checks.append(dict(profile=profile, shared_buffers=expected, status='PASS',
                               restart=True, second_branch=True, suspend_resume=True, conflict_rejected=True))
        finally:
            if (root / 'control.sock').exists(): cell.stop()
    (output / 'result.json').write_text(json.dumps(dict(status='PASS', checks=checks), indent=2) + '\n')
    print(json.dumps(dict(status='PASS', checks=checks)))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args(); run(args.release.resolve(), args.output.resolve())
