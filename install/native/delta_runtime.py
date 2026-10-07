"""Require a source-built Delta artifact matching the reviewed build contract."""
import json
from pathlib import Path
import shutil
import re
import zipfile
from email.parser import BytesParser

from analytics import digest

ROOT = Path(__file__).resolve().parents[2]


def verify_report(report, target, *, root=ROOT):
    pin = json.loads((root / 'components/deltalake-source.lock.json').read_text())
    expected = dict(schema_version=1, repository=pin['repository'], commit=pin['commit'],
                    version=pin['version'], target=target, inputs=pin['inputs'], maturin=pin['maturin'],
                    patch=pin['patch'], cargo_lock=pin['cargo_lock'], profile=pin['profile'],
                    source_lock_sha256=digest(root / 'components/deltalake-source.lock.json'),
                    builder_script_sha256=digest(root / 'components/build-deltalake.py'))
    if any(report.get(k) != v for k, v in expected.items()) or report.get('rustc', '').split()[1:2] != [pin['rust']]:
        raise ValueError('Delta artifact differs from reviewed source/build inputs')
    wheel = report.get('wheel', {})
    suffix = {'linux-x86_64': 'linux_x86_64.whl', 'macos-arm64': 'macosx_15_0_arm64.whl'}[target]
    if not (isinstance(wheel.get('file'), str) and '/' not in wheel['file']
            and wheel['file'].startswith('deltalake-' + pin['version'] + '-') and wheel['file'].endswith(suffix)
            and re.fullmatch(r'[0-9a-f]{64}', wheel.get('sha256', ''))):
        raise ValueError('Delta wheel target or digest mismatch')
    return {**expected, 'wheel': wheel, 'rustc': report['rustc']}


def verify(directory, target, *, root=ROOT):
    pin = json.loads((root / 'components/deltalake-source.lock.json').read_text())
    report = json.loads((directory / 'deltalake-build.json').read_text())
    verify_report(report, target, root=root)
    files = report.get('files', {})
    actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() and p.name != 'deltalake-build.json'}
    if actual != set(files) or not {'Cargo.lock', 'LICENSE.txt', 'dependencies.json', 'bounded-merge.patch'} <= actual:
        raise ValueError('Delta artifact inventory is incomplete')
    for name, checksum in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink() or digest(path) != checksum:
            raise ValueError('Delta artifact file checksum or path mismatch')
    wheel = directory / report['wheel']['file']
    if wheel.name != report['wheel']['file'] or files.get(wheel.name) != report['wheel']['sha256']:
        raise ValueError('Delta wheel differs from source artifact')
    if digest(directory / 'Cargo.lock') != pin['cargo_lock']['sha256'] or digest(directory / 'LICENSE.txt') != pin['inputs']['LICENSE.txt']:
        raise ValueError('Delta source inventory mismatch')
    if digest(directory / 'bounded-merge.patch') != pin['patch']['sha256']:
        raise ValueError('Delta patch inventory mismatch')
    with zipfile.ZipFile(wheel) as archive:
        metadata = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        native = [n for n in archive.namelist() if n.startswith('deltalake/_internal') and n.endswith('.so')]
        if len(metadata) != 1 or len(native) != 1:
            raise ValueError('Delta wheel lacks native extension or package metadata')
        package = BytesParser().parsebytes(archive.read(metadata[0]))
        if package['Name'] != 'deltalake' or package['Version'] != pin['version']:
            raise ValueError('Delta wheel package identity mismatch')
    return wheel, report


def install_inputs(destination, target):
    directory = ROOT / 'build/deltalake-artifacts' / target
    wheel, report = verify(directory, target)
    provenance = destination / 'provenance/deltalake'
    shutil.copytree(directory, provenance, ignore=shutil.ignore_patterns('*.whl', 'licenses'))
    shutil.copytree(directory / 'licenses', destination / 'licenses/deltalake')
    shutil.copy2(directory / 'LICENSE.txt', destination / 'licenses/deltalake/LICENSE.txt')
    shutil.copy2(ROOT / 'components/deltalake-source.lock.json', provenance / 'source.lock.json')
    return wheel, report
