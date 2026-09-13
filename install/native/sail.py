"""Require a source-built Sail artifact matching the reviewed build contract."""
import json
from pathlib import Path
import shutil
import zipfile
from email.parser import BytesParser

from analytics import digest

ROOT = Path(__file__).resolve().parents[2]


def verify(directory, target, *, root=ROOT):
    pin = json.loads((root / 'components/sail-source.lock.json').read_text())
    report = json.loads((directory / 'sail-build.json').read_text())
    expected = dict(schema_version=1, repository=pin['repository'], commit=pin['commit'], source_dirty=False,
                    version=pin['version'], target=target, inputs=pin['inputs'], maturin=pin['maturin'],
                    protoc=pin['targets'][target]['protoc'], profile=pin['profile'],
                    source_lock_sha256=digest(root / 'components/sail-source.lock.json'),
                    builder_script_sha256=digest(root / 'components/build-sail.py'))
    if any(report.get(k) != v for k, v in expected.items()) or report.get('rustc', '').split()[1:2] != [pin['rust']]:
        raise ValueError('Sail artifact differs from reviewed source/build inputs')
    files = report.get('files', {})
    actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() and p.name != 'sail-build.json'}
    if actual != set(files) or not {'Cargo.lock', 'LICENSE', 'dependencies.json'} <= actual:
        raise ValueError('Sail artifact inventory is incomplete')
    for name, checksum in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink() or digest(path) != checksum:
            raise ValueError('Sail artifact file checksum or path mismatch')
    wheel = directory / report['wheel']['file']
    if wheel.name != report['wheel']['file'] or files.get(wheel.name) != report['wheel']['sha256']:
        raise ValueError('Sail wheel differs from source artifact')
    if digest(directory / 'Cargo.lock') != pin['inputs']['Cargo.lock'] or digest(directory / 'LICENSE') != pin['inputs']['LICENSE']:
        raise ValueError('Sail source inventory mismatch')
    with zipfile.ZipFile(wheel) as archive:
        metadata = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        native = [n for n in archive.namelist() if n.startswith('pysail/_native') and n.endswith('.so')]
        if len(metadata) != 1 or len(native) != 1:
            raise ValueError('Sail wheel lacks native extension or package metadata')
        package = BytesParser().parsebytes(archive.read(metadata[0]))
        if package['Name'] != 'pysail' or package['Version'] != pin['version']:
            raise ValueError('Sail wheel package identity mismatch')
    return wheel, report


def install_inputs(destination, target):
    directory = ROOT / 'build/sail-artifacts' / target
    wheel, report = verify(directory, target)
    provenance = destination / 'provenance/sail'
    shutil.copytree(directory, provenance, ignore=shutil.ignore_patterns('*.whl', 'licenses'))
    shutil.copytree(directory / 'licenses', destination / 'licenses/sail')
    shutil.copy2(directory / 'LICENSE', destination / 'licenses/sail/LICENSE')
    shutil.copy2(ROOT / 'components/sail-source.lock.json', provenance / 'source.lock.json')
    return wheel, report
