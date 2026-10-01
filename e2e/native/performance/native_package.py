#!/usr/bin/env python3
"""Replace only the native binary in an immutable, already profiled package."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def overlay(base, binary, destination, revision, proof):
    assert len(revision) == 40 and all(c in '0123456789abcdef' for c in revision)
    assert not destination.exists() and not proof.exists()
    original = sha(base/'release.json')
    manifest = json.loads((base/'release.json').read_text())
    # Verify every shared dependency, not merely the manifest's own hash.
    for name, entry in manifest['files'].items():
        path = base/name
        assert sha(path) == entry['sha256'], name
        assert bool(path.stat().st_mode & 0o111) == entry['executable'], name
    subprocess.run(['cp', '-al', str(base), str(destination)], check=True)
    target = destination/'bin/supabricks'
    target.unlink()  # Break the hardlink before replacing any bytes/permissions.
    shutil.copyfile(binary, target)
    target.chmod(0o755)
    before = manifest['files']['bin/supabricks']['sha256']
    manifest['files']['bin/supabricks'] = dict(sha256=sha(target), executable=True)
    receipt = dict(base_release_sha256=original, native_revision=revision,
                   binary_sha256=sha(target), previous_binary_sha256=before,
                   changed_payload_files=['bin/supabricks'],
                   verified_shared_payload_files=len(manifest['files'])-1,
                   signed_release=False)
    manifest['diagnostic_native_overlay'] = receipt
    path = destination/'release.json'
    path.unlink()
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n')
    assert sha(base/'release.json') == original
    assert sha(base/'bin/supabricks') == before
    proof.write_text(json.dumps(dict(receipt, release_sha256=sha(path)), indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'binary', 'destination', 'proof'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    overlay(args.base.resolve(), args.binary.resolve(), args.destination.resolve(),
            args.revision, args.proof.resolve())
