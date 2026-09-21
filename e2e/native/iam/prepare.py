#!/usr/bin/env python3
"""Verify and prepare IAM00's fixed product archive and full gVisor distribution."""
import argparse
import json
from pathlib import Path
import tarfile
import urllib.request
from common import PINS, command, digest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    assert digest(args.archive)==PINS['platform']['sha256'],'platform archive differs from qualified pin'
    args.output.mkdir(parents=True,exist_ok=False)
    with tarfile.open(args.archive) as archive:
        archive.extractall(args.output,filter='data')
    release=args.output/'supabricks'
    inventory=json.loads((release/'release.json').read_text())
    for name,entry in inventory['files'].items():
        file=release/name
        assert file.resolve().is_relative_to(release.resolve()),'inventory path escapes release'
        assert digest(file)==entry['sha256'],'release inventory mismatch: '+name
    tools=args.output/'gvisor'; tools.mkdir()
    archive=tools/'gvisor.tar.bz2'
    with urllib.request.urlopen(PINS['gvisor']['url'],timeout=60) as src, archive.open('wb') as dst:
        import shutil
        shutil.copyfileobj(src,dst)
    assert digest(archive,'sha512')==PINS['gvisor']['sha512'],'gVisor distribution differs from pin'
    with tarfile.open(archive) as tar: tar.extractall(tools,filter='data')
    files={str(p.relative_to(tools)):digest(p) for p in sorted(tools.rglob('*')) if p.is_file()}
    (tools/'verified.json').write_text(json.dumps(files,indent=2)+'\n')
    for image in (PINS['keycloak']['image'],PINS['rootfs']):
        command('docker','pull',image,timeout=300)
    print(args.output)


if __name__=='__main__': main()
