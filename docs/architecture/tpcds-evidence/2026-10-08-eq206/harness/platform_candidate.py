"""Exact, source-bound native/session-worker overlay for engineering qualification."""
import json
from pathlib import Path
import re
import subprocess

from inputs import sha

ROOT = Path(__file__).resolve().parents[2]
WORKER = 'python/analytics/session.py'


def replacement_files(artifact):
    report = json.loads((artifact / 'platform-build.json').read_text())
    commit = report['commit']
    assert re.fullmatch('[0-9a-f]{40}', commit), 'invalid platform source revision'
    assert report['source_dirty'] is False, 'dirty platform source'
    assert report['command'] == ['cargo', 'build', '--release', '--locked', '-p', 'supabricks-local']
    assert set(report['files']) == {'bin/supabricks', WORKER}, 'unexpected platform payload'
    files = {}
    for name, expected in report['files'].items():
        path = artifact / name
        assert sha(path) == expected, 'platform artifact tampered: ' + name
        files[name] = path.read_bytes()
    # Bind the interpreted worker and dependency lock to the recorded Git source,
    # not merely hashes supplied by the artifact producer. The binary build and
    # toolchain receipt remains an engineering attestation, not a signed release.
    source = subprocess.check_output(['git', 'show', commit + ':' + WORKER], cwd=ROOT)
    assert files[WORKER] == source, 'worker differs from committed platform source'
    import hashlib
    lock = subprocess.check_output(['git', 'show', commit + ':Cargo.lock'], cwd=ROOT)
    assert hashlib.sha256(lock).hexdigest() == report['cargo_lock_sha256']
    return files, report
