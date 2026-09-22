#!/usr/bin/env python3
"""Prepare private isolated-runtime inputs for this verified installed release.

Run as the dedicated server owner. Fetch the reviewed gVisor archive and Docker
rootfs image during staging; this command performs no downloads or image pulls.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def digest(path, algorithm='sha256'):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,algorithm).hexdigest()


def prepare(release, archive, output, config):
    release=release.resolve();output=output.resolve();config=config.resolve()
    if config.exists():raise ValueError('choose a new execution configuration path')
    os.umask(0o077)
    pins=json.loads((release/'share/governed/execution-runtime.lock.json').read_text())
    manifest=json.loads((release/'release.json').read_text())
    if manifest['target']!='linux-x86_64':raise ValueError('governed execution requires Linux x86_64')
    verified=json.loads(subprocess.check_output([str(release/'bin/supabricks'),'installation','verify'],text=True))
    if verified['identity']!=digest(release/'release.json'):raise ValueError('release changed during verification')
    if digest(archive,'sha512')!=pins['gvisor']['sha512']:raise ValueError('gVisor archive differs from reviewed pin')
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    tools=output/'gvisor';tools.mkdir(mode=0o700)
    shutil.copyfile(archive,tools/'gvisor.tar.bz2')
    with tarfile.open(archive) as tar:tar.extractall(tools,filter='data')
    inventory={str(path.relative_to(tools)):digest(path) for path in sorted(tools.rglob('*')) if path.is_file()}
    (tools/'verified.json').write_text(json.dumps(inventory,indent=2)+'\n')
    if digest(tools/'verified.json')!=pins['gvisor']['inventory_sha256']:raise ValueError('gVisor extracted inventory differs')
    rootfs=output/'rootfs.tar'
    container=subprocess.check_output(['docker','create','--pull=never','--network=none',pins['rootfs']],text=True).strip()
    try:subprocess.run(['docker','export','-o',str(rootfs),container],check=True)
    finally:subprocess.run(['docker','rm','-f',container],check=True,stdout=subprocess.DEVNULL)
    value=dict(release=str(release),inventory_sha256=verified['identity'],tools=str(tools),tools_sha256=digest(tools/'verified.json'),
               rootfs=str(rootfs),rootfs_sha256=digest(rootfs))
    fd=os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2);stream.write('\n')
    return dict(release_identity=verified['identity'],execution_config_sha256=digest(config),
                gvisor_inventory_sha256=value['tools_sha256'],rootfs_sha256=value['rootfs_sha256'],rootfs_image=pins['rootfs'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,default=Path(__file__).resolve().parents[2])
    for name in ['gvisor-archive','output','config']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.release,args.gvisor_archive,args.output,args.config)))
