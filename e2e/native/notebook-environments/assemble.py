#!/usr/bin/env python3
"""Builder-only NE01 derivative; never modify a release or enable product installs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request
from urllib.parse import unquote, urlparse

from packaging.markers import Marker
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[3]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, sha256, destination):
    if not destination.exists():
        with urllib.request.urlopen(url, timeout=120) as response, destination.open('wb') as out:
            shutil.copyfileobj(response, out)
    if digest(destination) != sha256:
        raise ValueError('input checksum mismatch: ' + destination.name)


def main(args):
    baseline, out = args.release.resolve(), args.output.resolve()
    assert not out.exists(), 'use a fresh output directory'
    subprocess.run([str(baseline / 'bin/supabricks'), 'installation', 'verify'], check=True)
    out.mkdir(parents=True)
    service = out / 'service'
    shutil.copytree(baseline, service)
    wheels = out / 'kernel-wheels'
    wheels.mkdir()
    inputs = out / 'build-inputs'
    inputs.mkdir()
    uv_pin = json.loads((ROOT / 'components/notebook-uv.lock.json').read_text())
    archive = inputs / 'uv.tar.gz'
    fetch(**uv_pin['targets'][args.target], destination=archive)
    with tarfile.open(archive) as tar:
        tar.extractall(inputs / 'uv', filter='data')
    (out / 'bin').mkdir()
    shutil.copy2(next((inputs / 'uv').glob('*/uv')), out / 'bin/uv')
    notices = out / 'licenses/uv'
    notices.mkdir(parents=True)
    for name, pin in uv_pin['notices'].items():
        fetch(**pin, destination=notices / name)
    lock_path = ROOT / 'python/notebooks/uv.lock'
    assert digest(baseline / 'python/notebooks/uv.lock') == digest(lock_path), 'requalify the baseline after changing the service lock'
    packages = {p['name']: p for p in tomllib.loads(lock_path.read_text())['package']}
    # Traverse the existing qualified graph, evaluating target markers. No
    # resolution or implicit upgrade; these two roots include Spark's clients.
    closure = set()
    def visit(name):
        if name in closure:
            return
        closure.add(name)
        for dep in packages[name].get('dependencies', []):
            if 'marker' not in dep or Marker(dep['marker']).evaluate():
                visit(dep['name'])
    for name in ['ipykernel', 'pyspark-client']:
        visit(name)
    tags = set(sys_tags())
    sources, selected, registry = {}, {}, {}
    def wheel(candidates):
        for item in candidates:
            name = unquote(Path(urlparse(item['url']).path).name)
            if parse_wheel_filename(name)[3] & tags:
                fetch(item['url'], item.get('sha256') or item['hash'].removeprefix('sha256:'), wheels / name)
                registry[name] = {'url': item['url'], 'sha256': digest(wheels / name)}
                return wheels / name
        raise ValueError('no qualified wheel for this target')
    for name in sorted(closure):
        package = packages[name]
        if package.get('wheels'):
            artifact = wheel(package['wheels'])
        else:
            assert name == 'pyspark-client', 'only the existing qualified Spark sdist may be built'
            source = package['sdist']
            path = inputs / Path(urlparse(source['url']).path).name
            fetch(source['url'], source['hash'].removeprefix('sha256:'), path)
            # All build tools already exist at exact versions in the copied
            # service. uv may neither fetch build dependencies nor Python.
            subprocess.run([str(out / 'bin/uv'), '--no-config', '--offline', '--no-python-downloads',
                            'build', '--wheel', '--no-build-isolation', '--python',
                            str(service / 'python/runtime/bin/python3.12'), '--out-dir', str(wheels), str(path)],
                           check=True, env=dict(os.environ, SOURCE_DATE_EPOCH='1788998400'))
            artifact = next(wheels.glob('pyspark_client-*.whl'))
            sources[name] = {'sdist': source, 'build_tools': {n: packages[n]['version'] for n in
                            ['setuptools', 'wheel', 'packaging']}, 'output_sha256': digest(artifact)}
        selected[name] = {'version': package['version'], 'wheel': artifact.name, 'sha256': digest(artifact)}
    fixtures = json.loads(Path(__file__).with_name('fixtures.lock.json').read_text())
    extras = {}
    for requirement, candidates in fixtures.items():
        artifact = wheel(candidates)
        extras[requirement] = {'wheel': artifact.name, 'sha256': digest(artifact)}
    requirements = ''.join(f"{name}=={p['version']} --hash=sha256:{p['sha256']}\n" for name, p in selected.items())
    (out / 'base.lock').write_text(requirements)
    (out / 'protected.txt').write_text(''.join(f"{name}=={p['version']}\n" for name, p in selected.items()))
    for label, version in [('a', '4.13.0'), ('b', '4.14.0')]:
        additional = [f'humanize=={version}', 'xxhash==3.5.0']
        (out / f'{label}.lock').write_text(requirements + ''.join(
            f"{r} --hash=sha256:{extras[r]['sha256']}\n" for r in additional))
    # This dispatcher belongs only to the derivative probe. Rust, Jupyter,
    # bounded Session and bootstrap remain the baseline's unmodified bytes.
    wrapper = service / 'python/analytics/python'
    wrapper.write_text('''#!/bin/bash
set -euo pipefail
directory=$(cd -P "$(dirname "$0")" && pwd)
unset PYTHONHOME PYTHONPATH PYTHONUSERBASE PYTHONSTARTUP PYTHONINSPECT
export PYTHONDONTWRITEBYTECODE=1
if [[ "${4:-}" == "$directory/../notebooks/kernel.py" || "${4:-}" == "${directory%/analytics}/notebooks/kernel.py" ]]; then
  IFS= read -r kernel_python < "$PWD/.ne01-python"
  exec "$kernel_python" -E -s -B "$@"
fi
exec "$directory/../runtime/bin/python3.12" -E -s -B "$@"
''')
    manifest = json.loads((service / 'release.json').read_text())
    manifest['files']['python/analytics/python']['sha256'] = digest(wrapper)
    manifest['provenance']['ne01_probe_only'] = {'baseline_release_sha256': digest(baseline / 'release.json')}
    (service / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
    subprocess.run([str(service / 'bin/supabricks'), 'installation', 'verify'], check=True)
    report = dict(target=args.target, uv=uv_pin, uv_binary_sha256=digest(out / 'bin/uv'),
                  service_baseline_sha256=digest(baseline / 'release.json'),
                  notebook_lock_sha256=digest(lock_path), roots=['ipykernel', 'pyspark-client'],
                  packages=selected, extras=extras, source_builds=sources, registry_wheels=registry,
                  wheel_bytes=sum(p.stat().st_size for p in wheels.iterdir()),
                  uv_bytes=(out / 'bin/uv').stat().st_size,
                  files={str(p.relative_to(out)): digest(p) for parent in [wheels, out / 'licenses', out / 'bin']
                         for p in parent.rglob('*') if p.is_file()})
    (out / 'probe.json').write_text(json.dumps(report, indent=2) + '\n')
    # Builder inputs are retained separately from the relocatable runtime.
    shutil.move(inputs, out.with_name(out.name + '-build-inputs'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', choices=['linux-x86_64', 'macos-arm64'], required=True)
    main(parser.parse_args())
