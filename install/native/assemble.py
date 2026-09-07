#!/usr/bin/env python3
"""Build the complete immutable local analytical preview on the target machine."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'components'))
spec = importlib.util.spec_from_file_location('verify', ROOT / 'components/verify-native-bundle.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def output(*args):
    return subprocess.check_output(list(map(str, args)), cwd=ROOT, text=True).strip()


def assemble(args):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?', args.version):
        raise ValueError('version must be a release tag, e.g. v0.1.0-alpha.1')
    native = 'macos-arm64' if (platform.system(), platform.machine()) == ('Darwin', 'arm64') else 'linux-x86_64'
    if args.target != native:
        raise ValueError('assembly and loader checks must run on the target architecture')
    lock = json.loads((ROOT / 'components/components.lock.json').read_text())
    verify.verify(args.engine, lock)
    helper_report = json.loads((args.helpers / 'helper-build.json').read_text())
    if helper_report['target'] != args.target:
        raise ValueError('helper target mismatch')
    release_lock = json.loads((ROOT / 'components/release-build.lock.json').read_text())
    pc_build = helper_report.get('process_compose_build') or {}
    if pc_build.get('check_for_updates') is not False or pc_build.get('source_commit') != release_lock['process_compose']['commit']:
        raise ValueError('release requires helpers prepared with --offline-runtime')
    cell_lock = json.loads((ROOT / 'components/native-cell.lock.json').read_text())
    if helper_report['source_commit'] != cell_lock['seaweedfs_sqlite']['commit']:
        raise ValueError('SeaweedFS source pin mismatch')
    for name in ['weed', 'process-compose']:
        if digest(args.helpers / name) != helper_report['binaries'][name]:
            raise ValueError('helper binary differs from build report: ' + name)
    destination = args.output.resolve() / f'supabricks-{args.version}-{args.target}'
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(args.engine, destination / 'engine', symlinks=False)
    (destination / 'bin').mkdir()
    (destination / 'helpers').mkdir()
    (destination / 'licenses').mkdir()
    shutil.copy2(args.binary, destination / 'bin/supabricks')
    for name in ['weed', 'process-compose']:
        shutil.copy2(args.helpers / name, destination / 'helpers' / name)
    # The CLI's only non-OS ELF dependency is libgcc_s, already in the engine.
    # Never ship a release that silently loads libraries from the build machine.
    if args.target == 'linux-x86_64':
        subprocess.run(['patchelf', '--set-rpath', '$ORIGIN/../engine/lib', str(destination / 'bin/supabricks')], check=True)
        listing = output('ldd', destination / 'bin/supabricks')
        for line in listing.splitlines():
            if 'not found' in line:
                raise ValueError('unresolved CLI library: ' + line)
            match = re.search(r'(\S+) => (\S+)', line)
            if match and match[1] not in {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1'}:
                if not Path(match[2]).resolve().is_relative_to(destination):
                    raise ValueError('CLI library outside release: ' + line)
    else:
        listing = output('otool', '-L', destination / 'bin/supabricks')
        if any(not line.strip().startswith(('/usr/lib/', '/System/Library/')) for line in listing.splitlines()[1:]):
            raise ValueError('CLI has an unbundled non-system library')
        subprocess.run(['codesign', '--force', '--sign', '-', str(destination / 'bin/supabricks')], check=True)
    (destination / 'bin/psql').write_text('''#!/bin/bash
set -euo pipefail
self=$0
while [ -L "$self" ]; do
    directory=$(cd -P "$(dirname "$self")" && pwd)
    self=$(readlink "$self")
    case "$self" in /*) ;; *) self="$directory/$self" ;; esac
done
directory=$(cd -P "$(dirname "$self")" && pwd)
exec "$directory/../engine/pg_install/v17/bin/psql" "$@"
''')
    (destination / 'bin/psql').chmod(0o755)
    shutil.copy2(ROOT / 'LICENSE', destination / 'licenses/platform.txt')
    shutil.copy2(args.helpers / 'LICENSE', destination / 'licenses/process-compose.txt')
    shutil.copy2(args.helpers / 'SEAWEEDFS-LICENSE', destination / 'licenses/seaweedfs.txt')
    shutil.copytree(ROOT / 'examples/orders', destination / 'examples/orders', ignore=shutil.ignore_patterns('__pycache__', '.venv'))
    shutil.copytree(ROOT / 'agents', destination / 'agents', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT / 'docs/handbook/local-workflow.md', destination / 'WORKFLOW.md')
    shutil.copy2(ROOT / 'install/native/README.md', destination / 'INSTALL.md')
    shutil.copytree(ROOT / 'components/provenance', destination / 'provenance/engine-and-helpers')
    for name in ['components.lock.json', 'native-cell.lock.json', 'release-build.lock.json']:
        shutil.copy2(ROOT / 'components' / name, destination / 'provenance' / name)
    shutil.copy2(ROOT / 'Cargo.lock', destination / 'provenance/platform-Cargo.lock')
    shutil.copy2(args.helpers / 'helper-build.json', destination / 'provenance/helper-build.json')
    # Preserve declared licenses, exact sources and package identities even for
    # dependencies whose redistribution notices still need public-release audit.
    metadata = json.loads(output('cargo', 'metadata', '--locked', '--format-version', '1'))
    packages = []
    for package in metadata['packages']:
        source = Path(package['manifest_path']).parent
        record = {k: package[k] for k in ['name', 'version', 'source', 'license', 'repository']}
        record['notices'] = []
        for notice in sorted(source.iterdir()):
            if notice.is_file() and re.match(r'(?i)^(license|copying|copyright|notice)([._-]|$)', notice.name):
                target = destination / 'licenses/cargo' / f"{package['name']}-{package['version']}" / notice.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(notice, target)
                record['notices'].append(str(target.relative_to(destination)))
        packages.append(record)
    (destination / 'provenance/platform-dependencies.json').write_text(json.dumps(packages, indent=2) + '\n')
    if not args.postgres_only:
        from analytics import assemble_analytics
        assemble_analytics(destination, args.target)
    provenance = dict(
        platform_commit=output('git', 'rev-parse', 'HEAD'),
        platform_dirty=bool(output('git', 'status', '--porcelain', '--untracked-files=normal')),
        cargo_lock_sha256=digest(ROOT / 'Cargo.lock'),
        rustc=output('rustc', '--version'),
        builder=platform.platform(),
        engine_manifest_sha256=digest(args.engine / 'manifest.json'),
        helper_build=helper_report,
        minimum_os='glibc 2.39' if args.target == 'linux-x86_64' else 'macOS 15 arm64',
        distribution='localhost engineering alpha; public redistribution audit and publisher signing provisioning pending',
    )
    files = {}
    for path in sorted(destination.rglob('*')):
        if path.is_symlink():
            raise ValueError('release payload must not contain symlinks')
        if path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o755 if executable else 0o644)
            files[str(path.relative_to(destination))] = dict(sha256=digest(path), executable=executable)
    manifest = dict(format_version=1, version=args.version, target=args.target,
                    profile='local-postgres-alpha' if args.postgres_only else 'local-analytical-preview', provenance=provenance, files=files)
    (destination / 'release.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    subprocess.run([str(destination / 'bin/supabricks'), 'installation', 'verify'], check=True)
    subprocess.run([str(destination / 'bin/psql'), '--version'], check=True)
    archive = destination.with_suffix(destination.suffix + '.tar.gz')
    with tarfile.open(archive, 'w:gz', compresslevel=3, format=tarfile.PAX_FORMAT) as tar:
        # Explicit regular files avoid tar's hard-link deduplication.
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                info = tar.gettarinfo(str(path), arcname='supabricks/' + str(path.relative_to(destination)))
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ''
                with path.open('rb') as stream:
                    tar.addfile(info, stream)
    archive.with_name(archive.name + '.sha256').write_text(f'{digest(archive)}  {archive.name}\n')
    print(json.dumps(dict(archive=str(archive), sha256=digest(archive), bytes=archive.stat().st_size,
                          unpacked_bytes=sum(p.stat().st_size for p in destination.rglob('*') if p.is_file()))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, choices=['linux-x86_64', 'macos-arm64'])
    parser.add_argument('--version', default='v0.1.0-alpha.2')
    parser.add_argument('--postgres-only', action='store_true', help='explicit smaller profile without analytical dependencies')
    for name in ['binary', 'engine', 'helpers', 'output']:
        parser.add_argument('--' + name, required=True, type=Path)
    assemble(parser.parse_args())
