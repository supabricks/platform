#!/usr/bin/env python3
"""Build a pinned Supabricks Sail wheel; never fetch a prebuilt Sail package."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(source, target, output):
    pin = json.loads((ROOT / 'components/sail-source.lock.json').read_text())
    actual = {('Linux', 'x86_64'): 'linux-x86_64', ('Darwin', 'arm64'): 'macos-arm64'}.get((platform.system(), platform.machine()))
    if target != actual:
        raise ValueError('Sail requires a native target builder')
    source, output = source.resolve(), output.resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    if git('rev-parse', 'HEAD') != pin['commit'] or git('status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Sail checkout must be clean and match the reviewed commit')
    if {name: sha(source / name) for name in pin['inputs']} != pin['inputs']:
        raise ValueError('Sail source inputs differ from the lock')
    if importlib.metadata.version('maturin') != pin['maturin']:
        raise ValueError('incorrect Maturin builder')
    if output.exists():
        raise ValueError('use a fresh Sail artifact output directory')
    output.mkdir(parents=True)
    cache = ROOT / 'build/sail-tools' / target
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / 'protoc.zip'
    tool = pin['targets'][target]['protoc']
    if not archive.exists():
        with urllib.request.urlopen(tool['url'], timeout=120) as response, archive.open('wb') as stream:
            shutil.copyfileobj(response, stream)
    if sha(archive) != tool['sha256']:
        raise ValueError('protoc checksum mismatch')
    with zipfile.ZipFile(archive) as zipped:
        for name in zipped.namelist():
            if not (cache / name).resolve().is_relative_to(cache.resolve()):
                raise ValueError('unsafe protoc archive')
        zipped.extractall(cache)
    protoc = cache / 'bin/protoc'
    protoc.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CARGO_PROFILE_', 'RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS'))}
    env.update(pin['profile'], RUSTUP_TOOLCHAIN=pin['rust'], PROTOC=str(protoc),
               MACOSX_DEPLOYMENT_TARGET='15.0', SOURCE_DATE_EPOCH=git('show', '-s', '--format=%ct', 'HEAD'))
    def run(*args, capture=False):
        result = subprocess.run(list(map(str, args)), cwd=source, env=env, check=True,
                                stdout=subprocess.PIPE if capture else None, text=True)
        return result.stdout.strip() if capture else None
    rustc = run('rustc', '--version', capture=True)
    if rustc.split()[1] != pin['rust'] or run(protoc, '--version', capture=True) != 'libprotoc ' + pin['protoc_version']:
        raise ValueError('incorrect compiler version')
    started = time.monotonic()
    run(sys.executable, '-m', 'maturin', 'build', '--release', '--locked', '--compatibility', 'off',
        '--interpreter', sys.executable, '--out', output)
    wheels = list(output.glob('*.whl'))
    if len(wheels) != 1 or not wheels[0].name.startswith('pysail-' + pin['version'] + '-'):
        raise ValueError('unexpected Sail wheel inventory')
    if git('status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Sail build changed locked sources')
    # Record Cargo dependency identities and preserve available source notices.
    metadata = json.loads(run('cargo', 'metadata', '--locked', '--format-version', '1', capture=True))
    packages = []
    for package in metadata['packages']:
        record = {k: package[k] for k in ('name', 'version', 'source', 'license', 'repository')}
        record['notices'] = []
        for notice in sorted(Path(package['manifest_path']).parent.iterdir()):
            if notice.is_file() and re.match(r'(?i)^(license|copying|copyright|notice)([._-]|$)', notice.name):
                dest = output / 'licenses' / f"{package['name']}-{package['version']}" / notice.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(notice, dest)
                record['notices'].append(str(dest.relative_to(output)))
        packages.append(record)
    (output / 'dependencies.json').write_text(json.dumps(packages, indent=2) + '\n')
    for name in ('Cargo.lock', 'LICENSE'):
        shutil.copy2(source / name, output / name)
    report = dict(schema_version=1, repository=pin['repository'], commit=pin['commit'], source_dirty=False,
                  version=pin['version'], target=target, inputs=pin['inputs'], rustc=rustc,
                  maturin=pin['maturin'], protoc=tool, profile=pin['profile'],
                  builder_script_sha256=sha(Path(__file__)), source_lock_sha256=sha(ROOT / 'components/sail-source.lock.json'),
                  builder=platform.platform(), elapsed_seconds=time.monotonic() - started,
                  wheel=dict(file=wheels[0].name, sha256=sha(wheels[0])),
                  files={str(p.relative_to(output)): sha(p) for p in sorted(output.rglob('*')) if p.is_file()})
    (output / 'sail-build.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('commit', 'target', 'wheel', 'elapsed_seconds')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', required=True, choices=['linux-x86_64', 'macos-arm64'])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    build(args.source, args.target, args.output)
