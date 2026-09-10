#!/usr/bin/env python3
"""Verify the CI baseline, build NE01, then move it before offline creation."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tarfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--directory', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--target', required=True)
a = p.parse_args()
archive = a.directory / f'supabricks-v0.1.0-alpha.8-{a.target}.tar.gz'
with archive.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == Path(str(archive) + '.sha256').read_text().split()[0]
base = a.output / 'baseline'
base.mkdir(parents=True)
with tarfile.open(archive) as tar:
    tar.extractall(base, filter='data')
release = (base / 'supabricks').resolve()
subprocess.run([str(release / 'python/analytics/python'), str(Path(__file__).with_name('assemble.py')),
                '--release', str(release), '--output', str(a.output / 'assembled'), '--target', a.target], check=True)
(a.output / 'assembled').rename(a.output / 'relocated probe')
payload = a.output / f'kernel-payload-{a.target}.tar.gz'
with tarfile.open(payload, 'w:gz') as tar:
    for path in sorted((a.output / 'relocated probe').iterdir()):
        if path.name != 'service':
            tar.add(path, arcname='kernel-payload/' + path.name)
    # Retain exact source-only build input alongside the built wheel; uv's
    # archive and source/license identities are already pinned in probe.json.
    for path in (a.output / 'assembled-build-inputs').glob('pyspark*.tar.gz'):
        tar.add(path, arcname='builder-inputs/' + path.name)
with payload.open('rb') as stream:
    Path(str(payload) + '.sha256').write_text(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + payload.name + '\n')
