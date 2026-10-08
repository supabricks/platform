"""Reproduce installed incremental byte admission with sparse files, without a load.

This probes quota accounting only. Sparse files are not Delta data and do not
qualify physical capacity, throughput, publication or query correctness.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def run(release, output):
    analytics = release.resolve() / 'python/analytics'
    sys.path.insert(0, str(analytics))
    from capture.spool import CaptureError
    from incremental import storage
    from incremental.planning import inventory_snapshot
    if Path(storage.__file__).resolve() != analytics / 'incremental/storage.py':
        raise ValueError('probe must import the selected installed worker')
    report = dict(scope=__doc__, status='FAIL', cases=[],
                  worker_sha256=hashlib.sha256(Path(storage.__file__).read_bytes()).hexdigest(),
                  release_manifest_sha256=hashlib.sha256((release/'release.json').read_bytes()).hexdigest())
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('output already exists')
    os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='sf100-admission-', dir=output.parent) as temp:
            parent = Path(temp)
            root = parent / 'generation'
            root.mkdir(mode=0o700)
            path = root / 'sparse.parquet'
            for label, size, function, expected in [
                ('generation_below_1_gib', 1024**3-1, 'boundary', None),
                ('generation_above_1_gib', 1024**3+1, 'boundary', 'incremental_disk_budget'),
                ('planning_above_1_gib', 1024**3+1, 'planning', 'incremental_disk_budget'),
                ('retained_above_4_gib', 4*1024**3+1, 'retained', 'incremental_retention_budget'),
            ]:
                with path.open('wb') as stream:
                    stream.truncate(size)
                started = time.monotonic()
                error = None
                try:
                    if function == 'boundary':
                        storage.boundary(root, time.time()*1000+10000)
                    elif function == 'planning':
                        inventory_snapshot(root, time.time()*1000+10000)
                    else:
                        storage.retained_boundary(parent)
                except CaptureError as failure:
                    error = failure.code
                report['cases'].append(dict(name=label, logical_bytes=size,
                    allocated_bytes=path.stat().st_blocks*512, expected_error=expected,
                    observed_error=error, elapsed_seconds=time.monotonic()-started))
                if error != expected:
                    raise AssertionError(f'{label}: expected {expected}, observed {error}')
        report['status'] = 'PASS'
    finally:
        output.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.release, args.output)
