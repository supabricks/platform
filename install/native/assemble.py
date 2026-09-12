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


def console_source():
    entry = output('git', 'ls-files', '--stage', '--', 'console').split()
    if len(entry) != 4 or entry[0] != '160000':
        raise ValueError('console must be the pinned source submodule')
    pin = entry[1]
    if output('git', '-C', ROOT / 'console', 'rev-parse', 'HEAD') != pin:
        raise ValueError('console checkout differs from its platform gitlink; update the pin explicitly')
    if output('git', '-C', ROOT / 'console', 'status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('console source is dirty; commit it and update the platform gitlink before assembly')
    source = json.loads((ROOT / 'console/build/console-source.json').read_text())
    if (source.get('commit') != pin or source.get('dirty') is not False
            or source.get('package_lock_sha256') != digest(ROOT / 'console/package-lock.json')
            or source.get('manifest_sha256') != digest(ROOT / 'console/dist/console.json')):
        raise ValueError('console assets were built from different or dirty source; rebuild the pinned console')
    return source


def assemble(args):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?', args.version):
        raise ValueError('version must be a release tag, e.g. v0.1.0-alpha.1')
    native = 'macos-arm64' if (platform.system(), platform.machine()) == ('Darwin', 'arm64') else 'linux-x86_64'
    if args.target != native:
        raise ValueError('assembly and loader checks must run on the target architecture')
    console = ROOT / 'console/dist'
    if not (console / 'console.json').is_file():
        raise ValueError('build the console first: npm ci --prefix console && npm run build --prefix console')
    console_manifest = json.loads((console / 'console.json').read_text())
    frontend_source = console_source()
    if console_manifest.get('api_version') != 1 or 'index.html' not in console_manifest.get('files', {}):
        raise ValueError('console build is missing its API 1 manifest or entry point')
    console_files = {str(p.relative_to(console)): digest(p) for p in console.rglob('*') if p.is_file() and p.name != 'console.json'}
    if any(p.is_symlink() for p in console.rglob('*')) or console_files != console_manifest['files']:
        raise ValueError('console build inventory differs from its manifest')
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
    shutil.copytree(console, destination / 'share/console')
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
    (destination / 'share/ingest').mkdir(parents=True)
    shutil.copy2(ROOT / 'crates/local/src/ingest/receipt.sql', destination / 'share/ingest/receipt.sql')
    shutil.copy2(ROOT / 'LICENSE', destination / 'licenses/platform.txt')
    shutil.copy2(args.helpers / 'LICENSE', destination / 'licenses/process-compose.txt')
    shutil.copy2(args.helpers / 'SEAWEEDFS-LICENSE', destination / 'licenses/seaweedfs.txt')
    shutil.copytree(ROOT / 'examples/orders', destination / 'examples/orders', ignore=shutil.ignore_patterns('__pycache__', '.venv'))
    shutil.copytree(ROOT / 'examples/notebooks', destination / 'examples/notebooks')
    shutil.copy2(ROOT / 'docs/handbook/notebooks.md', destination / 'NOTEBOOKS.md')
    shutil.copytree(ROOT / 'agents', destination / 'agents', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT / 'docs/handbook/local-workflow.md', destination / 'WORKFLOW.md')
    shutil.copy2(ROOT / 'install/native/README.md', destination / 'INSTALL.md')
    shutil.copy2(ROOT / 'docs/handbook/recovery.md', destination / 'RECOVERY.md')
    shutil.copytree(ROOT / 'components/provenance', destination / 'provenance/engine-and-helpers')
    for name in ['components.lock.json', 'native-cell.lock.json', 'release-build.lock.json']:
        shutil.copy2(ROOT / 'components' / name, destination / 'provenance' / name)
    shutil.copy2(ROOT / 'Cargo.lock', destination / 'provenance/platform-Cargo.lock')
    shutil.copy2(ROOT / 'console/package-lock.json', destination / 'provenance/console-package-lock.json')
    shutil.copy2(ROOT / 'console/package.json', destination / 'provenance/console-package.json')
    shutil.copy2(ROOT / 'console/build/console-source.json', destination / 'provenance/console-source.json')
    # Preserve notices for all bundled runtime dependencies, including JupyterLab,
    # and Vite's generated loader. The lock retains exact source identities.
    frontend_lock = json.loads((ROOT / 'console/package-lock.json').read_text())
    supplemental = ROOT / 'console/licenses'
    license_index = json.loads((supplemental / 'index.json').read_text())
    shutil.copy2(supplemental / 'index.json', destination / 'provenance/console-license-sources.json')
    for relative, entry in frontend_lock['packages'].items():
        if not relative or (entry.get('dev') and relative != 'node_modules/vite'):
            continue
        package = ROOT / 'console' / relative
        target = destination / 'licenses/console' / relative
        target.mkdir(parents=True)
        notices = [p for p in package.iterdir() if p.name.lower().startswith(('license', 'notice', 'copying', 'copyright'))]
        if not notices:
            record = license_index.get(relative)
            if not record or record['version'] != entry['version']:
                raise ValueError('console dependency needs a reviewed license text: ' + relative)
            notice = supplemental / record['file']
            if digest(notice) != record['sha256']:
                raise ValueError('console supplemental license digest mismatch: ' + relative)
            notices = [notice]
        for notice in notices:
            if notice.is_dir():
                shutil.copytree(notice, target / notice.name)
            else:
                shutil.copy2(notice, target / notice.name)
        shutil.copy2(package / 'package.json', target / 'package.json')
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
        from notebooks import assemble_notebooks
        notebook_provenance = assemble_notebooks(destination, args.target)
        from environments import assemble_environments
        environment_provenance = assemble_environments(destination, args.target)
    if not args.postgres_only:
        (destination / 'python/ingest').mkdir()
        shutil.copy2(ROOT / 'python/ingest/worker.py', destination / 'python/ingest/worker.py')
    provenance = dict(
        console=dict(api_version=1, source=frontend_source, manifest_sha256=digest(console / 'console.json'),
                     package_lock_sha256=digest(ROOT / 'console/package-lock.json')),
        data_formats=dict(local_catalog=10, runtime_config=2, postgres_major=17, analytical_snapshot=1),
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
    if not args.postgres_only:
        provenance['ingestion'] = dict(protocol_version=1, worker_sha256=digest(ROOT / 'python/ingest/worker.py'))
        provenance['notebooks'] = notebook_provenance
        provenance['environments'] = environment_provenance
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
    parser.add_argument('--version', default='v0.1.0-alpha.9')
    parser.add_argument('--postgres-only', action='store_true', help='explicit smaller profile without analytical dependencies')
    for name in ['binary', 'engine', 'helpers', 'output']:
        parser.add_argument('--' + name, required=True, type=Path)
    assemble(parser.parse_args())
