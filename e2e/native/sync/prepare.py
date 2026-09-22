#!/usr/bin/env python3
"""Verify and unpack the exact alpha.35 archive for the SY00 CI probe."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

PINS = {
    'linux-x86_64': '46288e5198ff9771b949c478eb95b0e073a8d77402489a318e5e0a64fd7fb595',
    'macos-arm64': '7abcf4a94ff90c45bb5ddc29b45b35e23d4cb6e18a5ba0e1240bb9f61101a4eb',
}


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def verify(release):
    inventory=json.loads((release/'release.json').read_text())
    if inventory['version']!='v0.1.0-alpha.35':raise ValueError('unexpected release version')
    for name,entry in inventory['files'].items():
        path=release/name
        if not path.resolve().is_relative_to(release.resolve()) or digest(path)!=entry['sha256']:
            raise ValueError('release inventory mismatch: '+name)
    return inventory


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',required=True,type=Path)
    parser.add_argument('--target',required=True,choices=PINS)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if digest(args.archive)!=PINS[args.target]:raise ValueError('archive differs from qualified alpha.35')
    args.output.mkdir(parents=True,exist_ok=False)
    with tarfile.open(args.archive) as archive:archive.extractall(args.output,filter='data')
    release=args.output/'supabricks'
    inventory=verify(release)
    if inventory['target']!=args.target:raise ValueError('archive target mismatch')
    # Receipt ties the independently verified archive to extracted inputs.
    (args.output/'sy00-inputs.json').write_text(json.dumps({'target':args.target,
        'archive_sha256':PINS[args.target], 'release_manifest_sha256':digest(release/'release.json')},indent=2)+'\n')
    print(release)


if __name__=='__main__':main()
