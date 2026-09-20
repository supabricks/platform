#!/usr/bin/env python3
"""Verify the fixed qualification artifact before extracting the probe baseline."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',required=True)
    parser.add_argument('--artifact',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    pin=json.loads(Path(__file__).with_name('baseline.lock.json').read_text())
    assert digest(args.artifact)==pin['targets'][args.target]['sha256'], 'baseline artifact digest mismatch'
    args.output.mkdir(parents=True,exist_ok=False)
    name=f"supabricks-{pin['release']}-{args.target}.tar.gz"
    with zipfile.ZipFile(args.artifact) as archive:
        assert set(archive.namelist())=={name,name+'.sha256'}, archive.namelist()
        archive.extractall(args.output)
    archive=args.output/name
    expected=(args.output/(name+'.sha256')).read_text().split()[0]
    assert digest(archive)==expected
    with tarfile.open(archive) as tar: tar.extractall(args.output,filter='data')
    release=args.output/'supabricks'
    manifest=json.loads((release/'release.json').read_text())
    for name,entry in manifest['files'].items():
        path=release/name
        assert path.resolve().is_relative_to(release.resolve())
        assert digest(path)==entry['sha256'], name
    archive.unlink()  # unpacked closure remains; no duplicate large archive
    print(release)


if __name__=='__main__': main()
