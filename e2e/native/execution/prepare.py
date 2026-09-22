#!/usr/bin/env python3
"""Opt-in operator configuration for UC09.4's pinned isolated runtime.

Use IAM00 prepare.py first to verify the exact product/gVisor input archives.
This does not enable shared ingress or migrate the local-owner profile.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'iam'))
from common import PINS, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inputs = args.inputs.resolve()
    release, tools = inputs/'supabricks', inputs/'gvisor'
    assert digest(release/'release.json') == PINS['platform']['inventory_sha256']
    assert digest(tools/'gvisor.tar.bz2', 'sha512') == PINS['gvisor']['sha512']
    # Verify every executable against the archive, not a self-authored checksum file.
    import tarfile
    with tarfile.open(tools/'gvisor.tar.bz2') as archive:
        for entry in archive.getmembers():
            if entry.isfile():
                expected = hashlib.sha256(archive.extractfile(entry).read()).hexdigest()
                assert digest(tools/entry.name) == expected
    execution_pins = json.loads((Path(__file__).resolve().parents[3]/'components/execution-runtime.lock.json').read_text())
    assert execution_pins['platform'] == PINS['platform']
    assert execution_pins['rootfs'] == PINS['rootfs']
    assert digest(tools/'verified.json') == execution_pins['gvisor']['inventory_sha256']
    exported = inputs/('.rootfs-'+uuid.uuid4().hex+'.tar')
    container = subprocess.check_output(['docker','create',PINS['rootfs']], text=True).strip()
    try:
        subprocess.run(['docker','export','-o',str(exported),container], check=True)
    finally:
        subprocess.run(['docker','rm','-f',container], check=True, stdout=subprocess.DEVNULL)
    rootfs_hash = digest(exported)
    rootfs = inputs/('rootfs-'+rootfs_hash+'.tar')
    try:
        os.link(exported, rootfs)
    except FileExistsError:
        assert digest(rootfs) == rootfs_hash
    finally:
        exported.unlink()
    value = dict(release=str(release), inventory_sha256=digest(release/'release.json'),
        tools=str(tools), tools_sha256=digest(tools/'verified.json'),
        rootfs=str(rootfs), rootfs_sha256=digest(rootfs))
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2)
        out.write('\n')


if __name__ == '__main__':
    main()
