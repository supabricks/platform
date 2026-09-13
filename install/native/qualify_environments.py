#!/usr/bin/env python3
"""Verify/extract the exact native archive and run environment qualification in its packaged Python."""
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
parser.add_argument('--packages', action='store_true', help='Run NE04 online/offline package qualification')
parser.add_argument('--bundle-fixture', type=Path)
parser.add_argument('--lifecycle', action='store_true')
parser.add_argument('--previous-directory', type=Path)
parser.add_argument('--previous-version', default='v0.1.0-alpha.12')
parser.add_argument('--version', default='v0.1.0-alpha.14')
args = parser.parse_args()
if args.lifecycle and (args.packages or not args.previous_directory or not args.bundle_fixture):
    parser.error('--lifecycle requires --previous-directory and --bundle-fixture and excludes --packages')
archive = args.directory / f'supabricks-{args.version}-{args.target}.tar.gz'
with archive.open('rb') as stream:
    assert hashlib.file_digest(stream, 'sha256').hexdigest() == Path(str(archive) + '.sha256').read_text().split()[0]
root = Path(tempfile.mkdtemp(prefix='sb-ne02-archive-', dir='/tmp')).resolve()
with tarfile.open(archive) as tar:
    tar.extractall(root, filter='data')
release = root / 'supabricks'
subprocess.run([str(release / 'bin/supabricks'), 'installation', 'verify'], check=True)
for harness in (['lifecycle', 'index'] if args.lifecycle else ['packages'] if args.packages else ['manager', 'kernels']):
    report = args.report if harness in ('manager', 'packages', 'lifecycle') else args.report.with_name(args.report.stem + '-' + harness + '.json')
    result = subprocess.run([str(release / 'python/runtime/bin/python3.12'), '-I', '-B',
        str(Path(__file__).resolve().parents[2] / f'e2e/native/notebook-environments/{harness}.py'),
        '--release', str(release), '--report', str(report.resolve()),
        *(['--directory', str(args.directory.resolve()), '--previous-directory', str(args.previous_directory.resolve()),
           '--version', args.version, '--previous-version', args.previous_version,
           '--bundle-fixture', str(args.bundle_fixture.resolve())] if harness == 'lifecycle' else ['--bundle-fixture', str(args.bundle_fixture.resolve())] if harness == 'packages' and args.bundle_fixture else [])])
    if result.returncode:
        break
if result.returncode == 0:
    shutil.rmtree(root)
raise SystemExit(result.returncode)
