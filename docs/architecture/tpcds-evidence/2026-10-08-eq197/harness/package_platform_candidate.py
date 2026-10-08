#!/usr/bin/env python3
"""Package a source-bound worker/native overlay, retaining exact Sail binding."""
import argparse
import json
from pathlib import Path
import subprocess

from inputs import sha
from platform_candidate import replacement_files
from sail_candidate import validate


def package(base, original, destination, sail_artifact, platform_artifact, proof):
    assert not destination.exists() and not proof.exists()
    before = json.loads((base / 'release.json').read_text())
    identity = sha(base / 'release.json')
    verified = json.loads(subprocess.check_output([str(base / 'bin/supabricks'), 'installation', 'verify'], text=True))
    assert verified['verified'] and verified['identity'] == identity
    replacements, _ = replacement_files(platform_artifact)
    subprocess.run(['cp', '-al', str(base), str(destination)], check=True)
    after = json.loads(json.dumps(before))
    after['version'] = destination.name
    assert after['version'] != before['version']
    for name, data in replacements.items():
        assert name in before['files'], 'overlay cannot add new payload paths'
        path = destination / name
        mode = path.stat().st_mode
        path.unlink()
        path.write_bytes(data)
        path.chmod(mode)
        after['files'][name] = dict(sha256=sha(path), executable=bool(mode & 0o111))
    manifest = destination / 'release.json'
    manifest.unlink()
    manifest.write_text(json.dumps(after, indent=2, sort_keys=True) + '\n')
    changes = validate(json.loads((original / 'release.json').read_text()), after,
                       destination, sail_artifact, platform_artifact)
    assert sha(base / 'release.json') == identity
    for name in replacements:
        assert sha(base / name) == before['files'][name]['sha256']
    verified = json.loads(subprocess.check_output([str(destination / 'bin/supabricks'), 'installation', 'verify'], text=True))
    assert verified['verified'] and verified['identity'] == sha(manifest)
    proof.write_text(json.dumps(dict(base_release_identity=identity,
        original_load_release_identity=sha(original / 'release.json'), candidate_release_identity=sha(manifest),
        signed_release=False, installation_verification=verified, **changes), indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    names = ('base', 'original', 'destination', 'sail-artifact', 'platform-artifact', 'proof')
    for name in names:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    package(*(getattr(args, name.replace('-', '_')).resolve() for name in names))
