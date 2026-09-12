#!/usr/bin/env python3
"""Verify/extract the exact native archive and run NE02 in its packaged Python."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tarfile
import tempfile
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True)
parser.add_argument('--target', required=True)
parser.add_argument('--report', type=Path, required=True)
parser.add_argument('--version', default='v0.1.0-alpha.9')
args = parser.parse_args()
archive = args.directory / f'supabricks-{args.version}-{args.target}.tar.gz'
with archive.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == Path(str(archive) + '.sha256').read_text().split()[0]
root = Path(tempfile.mkdtemp(prefix='sb-ne02-archive-', dir='/tmp')).resolve()
with tarfile.open(archive) as tar:
    tar.extractall(root, filter='data')
release = root / 'supabricks'
subprocess.run([str(release / 'bin/supabricks'), 'installation', 'verify'], check=True)
result = subprocess.run([str(release / 'python/runtime/bin/python3.12'), '-I', '-B',
    str(Path(__file__).resolve().parents[2] / 'e2e/native/notebook-environments/manager.py'),
    '--release', str(release), '--report', str(args.report.resolve())])
if result.returncode == 0:
    shutil.rmtree(root)
raise SystemExit(result.returncode)
