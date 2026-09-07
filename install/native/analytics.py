#!/usr/bin/env python3
"""Assemble a relocatable analytical environment entirely on the build machine."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, sha256, path):
    if not path.exists():
        with urllib.request.urlopen(url, timeout=120) as response, path.open('wb') as out:
            shutil.copyfileobj(response, out)
    if digest(path) != sha256:
        raise ValueError('analytical build input checksum mismatch: ' + path.name)


def check_loaders(runtime, destination, target):
    inspected = 0
    copied = {}
    paths = list(runtime.rglob('*'))
    for path in paths:
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            magic = stream.read(4)
        if target == 'linux-x86_64' and magic == b'\x7fELF':
            old = subprocess.check_output(['patchelf', '--print-rpath', str(path)], text=True).strip()
            engine = '$ORIGIN/' + os.path.relpath(destination / 'engine/lib', path.parent)
            private = '$ORIGIN/' + os.path.relpath(runtime / 'lib', path.parent)
            subprocess.run(['patchelf', '--set-rpath', ':'.join(filter(None, [old, private, engine])), str(path)], check=True)
            listing = subprocess.check_output(['ldd', str(path)], text=True)
            for line in listing.splitlines():
                if 'not found' in line:
                    raise ValueError('unresolved analytical library: ' + line)
                match = re.search(r'(\S+) => (\S+)', line)
                if match and match[1] not in {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1', 'libutil.so.1', 'libresolv.so.2'}:
                    if not Path(match[2]).resolve().is_relative_to(destination):
                        source = Path(match[2]).resolve()
                        bundled = runtime / 'lib' / match[1]
                        if bundled.exists():
                            continue  # Its parent's RUNPATH is patched later in this pass.
                        shutil.copy2(source, bundled)
                        paths.append(bundled)
                        copied[match[1]] = dict(builder_path=str(source), sha256=digest(source))
                        package = subprocess.check_output(['dpkg-query', '-S', str(source)], text=True).split(': ', 1)[0]
                        copied[match[1]]['package'] = subprocess.check_output(['dpkg-query', '-W', '-f=${Package} ${Version}', package], text=True)
                        notice = Path('/usr/share/doc') / package.split(':')[0] / 'copyright'
                        if not notice.is_file():
                            raise ValueError('missing system library notice: ' + str(notice))
                        notices = destination / 'licenses/analytical-system'
                        notices.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(notice, notices / (match[1] + '.txt'))
            inspected += 1
        elif target == 'macos-arm64' and magic in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
            listing = subprocess.check_output(['otool', '-L', str(path)], text=True)
            for line in listing.splitlines()[1:]:
                if line.rstrip().endswith(':'):
                    continue  # otool repeats the filename for each universal-binary architecture.
                dependency = line.strip().split(' (', 1)[0]
                if dependency.startswith('/') and not dependency.startswith(('/usr/lib/', '/System/Library/')):
                    raise ValueError('analytical library outside release: ' + dependency)
            inspected += 1
    if target == 'linux-x86_64':
        allowed = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1', 'libutil.so.1', 'libresolv.so.2'}
        for path in paths:
            if not path.is_file():
                continue
            with path.open('rb') as stream:
                if stream.read(4) != b'\x7fELF':
                    continue
            for line in subprocess.check_output(['ldd', str(path)], text=True).splitlines():
                match = re.search(r'(\S+) => (\S+)', line)
                if 'not found' in line or (match and match[1] not in allowed
                        and not Path(match[2]).resolve().is_relative_to(destination)):
                    raise ValueError('unbundled analytical dependency after relocation: ' + str(path) + ': ' + line)
    return dict(native_objects=inspected, copied_system_libraries=copied)


def assemble_analytics(destination, target):
    pin = json.loads((ROOT / 'components/analytical-runtime.lock.json').read_text())
    lock = tomllib.loads((ROOT / 'python/analytics/uv.lock').read_text())
    cache = ROOT / 'build/analytical-inputs' / target
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / 'python.tar.gz'
    fetch(**pin['targets'][target], path=archive)
    with tempfile.TemporaryDirectory(prefix='sb-python-', dir=cache) as temporary:
        tmp = Path(temporary)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter='data')
        # Flatten internal symlinks: the signed installer only accepts regular
        # files, and relocated releases must not reference builder paths.
        runtime = destination / 'python/runtime'
        shutil.copytree(tmp / 'python', runtime, symlinks=False)
    python = runtime / 'bin/python3.12'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'PIP_', 'UV_'))}
    env.update(PYTHONDONTWRITEBYTECODE='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_CONFIG_FILE=os.devnull)

    def run(*args):
        subprocess.run([str(python), '-I', '-B', *map(str, args)], env=env, check=True)

    requirements = (ROOT / 'python/analytics/requirements.lock').read_text()
    # Only Spark Connect is source-only. Every other package must use a wheel
    # whose bytes are in the existing qualified lock; no new resolution.
    blocks = re.split(r'(?=^[a-zA-Z0-9][a-zA-Z0-9_.-]*==)', requirements, flags=re.M)
    wheels = cache / 'wheels'
    wheels.mkdir(exist_ok=True)
    binary_requirements = cache / 'binary-requirements.txt'
    binary_requirements.write_text(''.join(b for b in blocks if not b.startswith('pyspark-client==')))
    run('-m', 'pip', 'download', '--require-hashes', '--no-deps', '--only-binary=:all:',
        '--dest', wheels, '-r', binary_requirements)
    run('-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile', '--find-links', wheels,
        'setuptools==80.9.0', 'wheel==0.45.1', 'packaging==25.0')
    spark = next(p for p in lock['package'] if p['name'] == 'pyspark-client')
    sdist = spark['sdist']
    source = cache / f"pyspark_client-{spark['version']}.tar.gz"
    fetch(sdist['url'], sdist['hash'].removeprefix('sha256:'), source)
    run('-m', 'pip', 'wheel', '--no-index', '--no-deps', '--no-build-isolation', '--wheel-dir', wheels, source)
    expected = {p['name'].replace('_', '-'): p['version'] for p in lock['package'] if 'registry' in p['source']}
    selected = sorted(wheels.glob('*.whl'))
    # A reused cache may contain old packages: never install anything beyond the
    # exact lock. pip's metadata check below additionally rejects duplicates.
    selected = [p for p in selected if p.name.split('-')[0].replace('_', '-') in expected
                and p.name.split('-')[1] == expected[p.name.split('-')[0].replace('_', '-')]]
    if len(selected) != len(expected):
        raise ValueError('wheel inventory differs from analytical lock')
    run('-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile', *selected)
    site = runtime / 'lib/python3.12/site-packages'
    # pip and its bootstrap wheel are build machinery, not runtime packages.
    for path in list(site.glob('pip*')) + [runtime / 'lib/python3.12/ensurepip']:
        if path.is_dir():
            shutil.rmtree(path)
    for path in runtime.rglob('__pycache__'):
        shutil.rmtree(path)
    for path in list((runtime / 'bin').iterdir()):
        if path.name != 'python3.12':
            path.unlink()
    # Python children used by Spark also preserve the immutable inventory.
    (site / 'sitecustomize.py').write_text('import sys\nsys.dont_write_bytecode = True\n')
    worker = destination / 'python/analytics'
    worker.mkdir()
    for name in ['export.py', 'session.py', 'shell.py', 'qualify.py', 'read_delta.py',
                 'uv.lock', 'requirements.lock', '.python-version']:
        shutil.copy2(ROOT / 'python/analytics' / name, worker / name)
    (destination / 'components').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'components/components.lock.json', destination / 'components/components.lock.json')
    wrapper = worker / 'python'
    wrapper.write_text('''#!/bin/bash
set -euo pipefail
directory=$(cd -P "$(dirname "$0")" && pwd)
unset PYTHONHOME PYTHONPATH PYTHONUSERBASE PYTHONSTARTUP PYTHONINSPECT
export PYTHONDONTWRITEBYTECODE=1
exec "$directory/../runtime/bin/python3.12" -E -s -B "$@"
''')
    wrapper.chmod(0o755)
    probe = worker / 'check_environment.py'
    probe.write_text('from qualify import environment\nimport json\nprint(json.dumps(environment()))\n')
    subprocess.run([str(wrapper), str(probe)], env=env, check=True)
    # Preserve upstream license texts, wheel provenance and the built client
    # digest. Wheel .dist-info licenses remain in the actual installed tree.
    loaders = check_loaders(runtime, destination, target)
    report = dict(native_objects_checked=loaders, python=pin, target=target, uv_lock_sha256=digest(ROOT / 'python/analytics/uv.lock'),
                  wheels={p.name: digest(p) for p in selected}, spark_sdist=sdist,
                  package_versions=expected)
    (destination / 'provenance/analytical-build.json').write_text(json.dumps(report, indent=2) + '\n')
    shutil.copy2(ROOT / 'components/analytical-runtime.lock.json', destination / 'provenance/analytical-runtime.lock.json')
    return report
