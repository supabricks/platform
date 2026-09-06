#!/usr/bin/env python3
"""Prepare checksum-pinned P03 helpers and verify an optional engine CI archive.

macOS requires Go only on the build host. Installed Supabricks needs no compiler.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('fetch', ROOT/'components/fetch-native-helpers.py')
fetch_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_module)


def extract(archive, output):
    with tarfile.open(archive) as tar:
        tar.extractall(output, filter='data')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['linux-x86_64', 'macos-arm64'])
    parser.add_argument('output', type=Path)
    parser.add_argument('--engine-archive', type=Path)
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT/'components/native-cell.lock.json').read_text())
    baseline = json.loads((ROOT/'components/components.lock.json').read_text())
    pc = next(c for c in baseline['components'] if c['id'] == 'process-compose')
    artifact = next(a for a in pc['artifacts'] if a['target'] == args.target)
    archive = output/'process-compose.tar.gz'
    fetch_module.fetch(artifact['url'], artifact['sha256'], archive)
    extract(archive, output)
    weed = lock['seaweedfs_sqlite']
    if args.target == 'linux-x86_64':
        archive = output/'seaweedfs-full.tar.gz'
        fetch_module.fetch(weed['linux_url'], weed['linux_sha256'], archive)
        extract(archive, output)
    else:
        if platform.system() != 'Darwin' or platform.machine() != 'arm64':
            parser.error('macOS helper build must run natively on Apple Silicon')
        version = subprocess.check_output(['go', 'version'], text=True).split()[2]
        if version != 'go' + weed['macos_build']['go']:
            parser.error('Go toolchain does not match the pinned build recipe')
        archive = output/'seaweedfs-source.tar.gz'
        fetch_module.fetch(weed['source_url'], weed['source_sha256'], archive)
        extract(archive, output)
        source = output / ('seaweedfs-' + weed['commit'])
        env = dict(os.environ, CGO_ENABLED='0', GOTOOLCHAIN='local')
        subprocess.run(['go', 'build', '-mod=readonly', '-trimpath', '-buildvcs=false', '-tags=sqlite',
                        '-ldflags=-s -w -X github.com/seaweedfs/seaweedfs/weed/util/version.COMMIT='+weed['commit'],
                        '-o', str(output/'weed'), './weed'], cwd=source, env=env, check=True)
        shutil.copy2(source/'LICENSE', output/'SEAWEEDFS-LICENSE')
    report = dict(target=args.target, source_commit=weed['commit'],
                  binaries={name: hashlib.sha256((output/name).read_bytes()).hexdigest() for name in ['weed', 'process-compose']})
    (output/'helper-build.json').write_text(json.dumps(report, indent=2)+'\n')
    if args.engine_archive:
        with args.engine_archive.open('rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() != lock['engine']['archives'][args.target]:
                raise ValueError('engine archive checksum differs from qualified E01 archive')
        extract(args.engine_archive, output)
        bundle = output/f'supabricks-engine-{args.target}'
        subprocess.run([os.sys.executable, str(ROOT/'components/verify-native-bundle.py'), str(bundle)], check=True)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
