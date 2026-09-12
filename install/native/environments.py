"""Build NE01-qualified offline environment components into the native release."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tomllib
from urllib.parse import urlparse, unquote
from analytics import digest, fetch

ROOT = Path(__file__).resolve().parents[2]


def assemble_environments(destination, target):
    # Run selection with the actual bundled interpreter and its packaging tags.
    subprocess.run([str(destination / 'python/runtime/bin/python3.12'), '-E', '-s', '-B',
                    str(Path(__file__).resolve()), '--release', str(destination), '--target', target], check=True)
    contract = destination / 'python/notebooks/kernel-contract.json'
    return {'version': 1, 'contract_sha256': digest(contract)}


def main(destination, target):
    from packaging.markers import Marker
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename
    cache = ROOT / 'build/environment-inputs' / target
    cache.mkdir(parents=True, exist_ok=True)
    pin = json.loads((ROOT / 'components/notebook-uv.lock.json').read_text())
    archive = cache / 'uv.tar.gz'
    fetch(**pin['targets'][target], path=archive)
    with tarfile.open(archive) as tar:
        tar.extractall(cache / 'uv', filter='data')
    uv = destination / 'helpers/uv'
    shutil.copy2(next((cache / 'uv').glob('*/uv')), uv)
    if target == 'linux-x86_64':
        subprocess.run(['patchelf', '--set-rpath', '$ORIGIN/../engine/lib', str(uv)], check=True)
        listing = subprocess.check_output(['ldd', str(uv)], text=True)
        for line in listing.splitlines():
            match = re.search(r'(\S+) => (\S+)', line)
            if 'not found' in line or (match and match[1] not in {'libc.so.6','libm.so.6','libpthread.so.0','libdl.so.2','librt.so.1'}
                    and not Path(match[2]).resolve().is_relative_to(destination)):
                raise ValueError('unbundled uv dependency: ' + line)
    else:
        listing = subprocess.check_output(['otool', '-L', str(uv)], text=True)
        if any(not line.strip().startswith(('/usr/lib/', '/System/Library/')) for line in listing.splitlines()[1:]):
            raise ValueError('unbundled uv dependency')
    notices = destination / 'licenses/uv'
    notices.mkdir(parents=True, exist_ok=True)
    for name, source in pin['notices'].items():
        fetch(**source, path=notices / name)
    lock_text = (ROOT / 'python/notebooks/uv.lock').read_text()
    packages = {p['name']: p for p in tomllib.loads(lock_text)['package']}
    chunks = re.split(r'(?=^\[\[package\]\]$)', lock_text, flags=re.M)
    blocks = {tomllib.loads(b)['package'][0]['name']: b for b in chunks[1:]}
    def closure(platform):
        selected = set()
        def visit(name):
            if name in selected:
                return
            selected.add(name)
            for dep in packages[name].get('dependencies', []):
                if not platform or 'marker' not in dep or Marker(dep['marker']).evaluate():
                    visit(dep['name'])
        visit('ipykernel')
        visit('pyspark-client')
        return selected
    selected, universal = closure(True), closure(False)
    wheels = destination / 'python/notebooks/wheelhouse'
    wheels.mkdir()
    tags = set(sys_tags())
    wheel_records = {}
    def download(candidates):
        for item in candidates:
            name = unquote(Path(urlparse(item['url']).path).name)
            if parse_wheel_filename(name)[3] & tags:
                sha = item.get('sha256') or item['hash'].removeprefix('sha256:')
                fetch(item['url'], sha, cache / name)
                shutil.copy2(cache / name, wheels / name)
                wheel_records[name] = dict(url=item['url'], sha256=sha)
                return name
        raise ValueError('missing qualified native wheel')
    wheel_names = {}
    analytical = json.loads((destination / 'provenance/analytical-build.json').read_text())
    for name in sorted(selected):
        p = packages[name]
        if p.get('wheels'):
            wheel_names[name] = download(p['wheels'])
        else:
            assert name == 'pyspark-client'
            # Reuse only the exact verified service wheel. A standalone builder
            # without that cache rebuilds the pinned source with the same tools;
            # record its resulting bytes rather than trusting a stale cache hit.
            artifact = next((ROOT / 'build/analytical-inputs' / target / 'wheels').glob('pyspark_client-' + p['version'] + '-*.whl'), None)
            if artifact is None or digest(artifact) != analytical['wheels'].get(artifact.name):
                source = p['sdist']
                source_path = cache / Path(urlparse(source['url']).path).name
                fetch(source['url'], source['hash'].removeprefix('sha256:'), source_path)
                built = cache / 'built'
                built.mkdir(exist_ok=True)
                subprocess.run([str(uv), '--no-config', '--offline', '--no-python-downloads', 'build',
                    '--wheel', '--no-build-isolation', '--python', str(destination / 'python/runtime/bin/python3.12'),
                    '--out-dir', str(built), str(source_path)], check=True)
                artifact = next(built.glob('pyspark_client-' + p['version'] + '-*.whl'))
            shutil.copy2(artifact, wheels / artifact.name)
            wheel_names[name] = artifact.name
            wheel_records[artifact.name] = dict(sdist=p['sdist'], sha256=digest(artifact),
                build_tools={n: packages[n]['version'] for n in ['setuptools', 'wheel', 'packaging']})
    fixture_pin = json.loads((ROOT / 'e2e/native/notebook-environments/fixtures.lock.json').read_text())
    extra_wheels = {r: download(candidates) for r, candidates in fixture_pin.items()}
    templates = {}
    for label, extras in [('base', []), ('fixture-a', ['humanize==4.13.0', 'xxhash==3.5.0']),
                          ('fixture-b', ['humanize==4.14.0', 'xxhash==3.5.0'])]:
        folder = destination / 'python/notebooks/environments' / label
        folder.mkdir(parents=True)
        roots = ['ipykernel==7.3.0', 'pyspark-client==4.2.0', *extras]
        config = '[project]\nname = "supabricks-notebook-kernel"\nversion = "0.1.0"\nrequires-python = "==3.12.13"\ndependencies = ' + json.dumps(roots) + '\n\n'
        # Keep the same qualified platform policy as the service project.
        config += (ROOT / 'python/notebooks/pyproject.toml').read_text().split('[tool.uv]')[1]
        config = config.replace('\npackage = false', '\n[tool.uv]\npackage = false', 1)
        (folder / 'pyproject.toml').write_text(config)
        text = chunks[0] + ''.join(blocks[n] for n in sorted(universal))
        for extra in extras:
            name, version = extra.split('==')
            text += f'\n[[package]]\nname = {json.dumps(name)}\nversion = {json.dumps(version)}\nsource = {{ registry = "https://pypi.org/simple" }}\nwheels = [\n'
            text += ''.join('    { url = ' + json.dumps(p['url']) + ', hash = ' + json.dumps('sha256:' + p['sha256']) + ' },\n' for p in fixture_pin[extra])
            text += ']\n'
        text += '\n[[package]]\nname = "supabricks-notebook-kernel"\nversion = "0.1.0"\nsource = { virtual = "." }\ndependencies = [\n'
        text += ''.join('    { name = ' + json.dumps(r.split('==')[0]) + ' },\n' for r in roots)
        text += ']\n\n[package.metadata]\nrequires-dist = [\n'
        text += ''.join('    { name = ' + json.dumps(r.split('==')[0]) + ', specifier = ' + json.dumps('==' + r.split('==')[1]) + ' },\n' for r in roots)
        text += ']\n'
        (folder / 'uv.lock').write_text(text)
        subprocess.run([str(uv), '--no-config', '--offline', '--no-python-downloads', 'export',
            '--project', str(folder), '--python', str(destination / 'python/runtime/bin/python3.12'),
            '--locked', '--no-emit-project', '--format', 'requirements-txt'], stdout=subprocess.DEVNULL, check=True)
        versions = {n: packages[n]['version'] for n in selected}
        names = dict(wheel_names)
        for extra in extras:
            name, version = extra.split('==')
            versions[name] = version
            names[name] = extra_wheels[extra]
        (folder / 'requirements.lock').write_text(''.join(f'{n}=={v} --hash=sha256:{digest(wheels / names[n])}\n' for n, v in sorted(versions.items())))
        templates[label] = dict(manifest=str((folder / 'pyproject.toml').relative_to(destination)),
            lock=str((folder / 'uv.lock').relative_to(destination)), requirements=str((folder / 'requirements.lock').relative_to(destination)),
            inputs=dict(manifest=digest(folder / 'pyproject.toml'), lock=digest(folder / 'uv.lock')), packages=versions)
    worker = destination / 'python/notebooks/environment-worker.py'
    shutil.copy2(ROOT / 'python/notebooks/environment-worker.py', worker)
    packages_worker = destination / 'python/notebooks/environment-packages.py'
    shutil.copy2(ROOT / 'python/notebooks/environment-packages.py', packages_worker)
    files = [uv, worker, packages_worker, destination / 'python/runtime/bin/python3.12',
             *wheels.iterdir(), *(destination / 'python/notebooks/environments').rglob('*')]
    contract = dict(version=1, target=target, python_version='3.12.13', templates=templates,
                    files={str(p.relative_to(destination)): digest(p) for p in files if p.is_file()})
    (destination / 'python/notebooks/kernel-contract.json').write_text(json.dumps(contract, indent=2) + '\n')
    (destination / 'provenance/environment-build.json').write_text(json.dumps(dict(uv=pin, wheels=wheel_records,
        notebook_lock_sha256=digest(ROOT / 'python/notebooks/uv.lock'), kernel_contract=contract), indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    main(args.release.resolve(), args.target)
