"""Add the N01-qualified notebook packages without changing analytical versions."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from analytics import digest, macho_dependencies

ROOT = Path(__file__).resolve().parents[2]


def assemble_notebooks(destination, target):
    runtime = destination / 'python/runtime'
    worker = destination / 'python/notebooks'
    worker.mkdir()
    base = tomllib.loads((ROOT / 'python/analytics/uv.lock').read_text())
    lock = tomllib.loads((ROOT / 'python/notebooks/uv.lock').read_text())
    versions = {p['name']: p['version'] for p in lock['package'] if 'registry' in p['source']}
    baseline = {p['name']: p['version'] for p in base['package'] if 'registry' in p['source']}
    if any(versions.get(k) != v for k, v in baseline.items()):
        raise ValueError('notebook runtime changes an analytical package version')
    before = {p.relative_to(runtime) for p in runtime.rglob('*') if p.is_file()}
    extra = set(versions) - set(baseline)
    requirements = (ROOT / 'python/notebooks/requirements.lock').read_text()
    blocks = re.split(r'(?=^[a-zA-Z0-9][a-zA-Z0-9_.-]*==)', requirements, flags=re.M)
    selected = worker / 'extra-requirements.lock'
    selected.write_text(''.join(b for b in blocks if b.split('==')[0] in extra))
    subprocess.run(['uv', 'pip', 'install', '--python', str(runtime / 'bin/python3.12'),
                    '--no-deps', '--require-hashes', '--only-binary', ':all:', '-r', str(selected)], check=True)
    adjustments = []
    allowed = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1', 'libutil.so.2', 'libresolv.so.2', 'libutil.so.1'}
    for path in runtime.rglob('*'):
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            magic = stream.read(4)
        if target == 'linux-x86_64' and magic == b'\x7fELF':
            if path.relative_to(runtime) not in before:
                old = subprocess.check_output(['patchelf', '--print-rpath', str(path)], text=True).strip()
                if any(part and not part.startswith('$ORIGIN') for part in old.split(':')):
                    raise ValueError('unexpected notebook loader path')
                paths = [old, '$ORIGIN/' + os.path.relpath(runtime / 'lib', path.parent),
                         '$ORIGIN/' + os.path.relpath(destination / 'engine/lib', path.parent)]
                subprocess.run(['patchelf', '--set-rpath', ':'.join(filter(None, paths)), str(path)], check=True)
            listing = subprocess.check_output(['ldd', str(path)], text=True)
            for line in listing.splitlines():
                match = re.search(r'(\S+) => (\S+)', line)
                if 'not found' in line or (match and match[1] not in allowed and not Path(match[2]).resolve().is_relative_to(destination)):
                    raise ValueError('unbundled notebook dependency: ' + str(path) + ': ' + line)
        elif target == 'macos-arm64' and magic in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
            listing = subprocess.check_output(['otool', '-l', str(path)], text=True)
            relative = path.relative_to(runtime).as_posix()
            if relative == 'lib/python3.12/site-packages/zmq/.dylibs/libsodium.26.dylib' and '/tmp/zmq/lib' in set(macho_dependencies(listing)):
                assert versions['pyzmq'] == '27.2.0' and path.relative_to(runtime) not in before
                subprocess.run(['install_name_tool', '-delete_rpath', '/tmp/zmq/lib', str(path)], check=True)
                subprocess.run(['codesign', '--force', '--sign', '-', str(path)], check=True)
                subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True)
                adjustments.append({'path': str(path.relative_to(destination)), 'removed_rpath': '/tmp/zmq/lib', 'signature': 'ad-hoc'})
                listing = subprocess.check_output(['otool', '-l', str(path)], text=True)
            for dependency in macho_dependencies(listing):
                if dependency.startswith('/') and not dependency.startswith(('/usr/lib/', '/System/Library/')):
                    raise ValueError('unbundled notebook dependency: ' + dependency)
    for cache in runtime.rglob('__pycache__'):
        shutil.rmtree(cache)
    for command in (runtime / 'bin').iterdir():
        if command.name != 'python3.12':
            command.unlink()
    for name in ['server.py', 'kernel.py', 'bootstrap.py', 'uv.lock', 'requirements.lock', '.python-version']:
        shutil.copy2(ROOT / 'python/notebooks' / name, worker / name)
    # The installed analytical validator checks the exact platform-applicable
    # union, while retaining its original immutable analytical lock.
    result = subprocess.check_output([str(destination / 'python/analytics/python'),
                                      str(destination / 'python/analytics/check_environment.py')], text=True)
    report = {'protocol_version': 1, 'uv_lock_sha256': digest(worker / 'uv.lock'),
              'analytical_versions_unchanged': baseline, 'environment': json.loads(result),
              'loader_adjustments': adjustments}
    (destination / 'provenance/notebook-build.json').write_text(json.dumps(report, indent=2) + '\n')
    return {'protocol_version': 1, 'uv_lock_sha256': report['uv_lock_sha256']}
