"""Verify and install only the reviewed source-built OSS UC/JRE closure."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(directory, target, *, root=ROOT):
    pin = json.loads((root/'components/unity-catalog-source.lock.json').read_text())
    report = json.loads((directory/'build.json').read_text())
    expected = dict(schema_version=2, target=target, source_commit=pin['commit'], source_dirty=False,
        source_pin_sha256=digest(root/'components/unity-catalog-source.lock.json'),
        dependency_lock_sha256=digest(root/'components/unity-catalog-maven.lock.json'),
        builder_script_sha256=digest(root/'components/build-unity-catalog.py'),
        java=pin['java'],sbt=pin['sbt'],source_inputs=pin['inputs'])
    if any(report.get(k) != v for k,v in expected.items()):
        raise ValueError('UC artifact differs from reviewed source/build inputs')
    files = report.get('files', {})
    actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() and p != directory/'build.json'}
    if actual != set(files) or not {'java/bin/java','classpath.json','dependencies.json','licenses/LICENSE','licenses/NOTICE'} <= actual:
        raise ValueError('UC runtime inventory is incomplete')
    if any(p.is_symlink() for p in directory.rglob('*')):
        raise ValueError('UC runtime contains a symlink')
    for name, checksum in files.items():
        path=directory/name
        if not path.resolve().is_relative_to(directory.resolve()) or digest(path) != checksum:
            raise ValueError('UC runtime checksum or path mismatch')
    classpath=json.loads((directory/'classpath.json').read_text())
    if not classpath or any(not p.startswith('jars/') or p not in files for p in classpath):
        raise ValueError('UC classpath is incomplete')
    if set(classpath) != {j['path'] for j in report['jars']}:
        raise ValueError('UC classpath differs from build inventory')
    for jar in report['jars']:
        if files.get(jar['path']) != jar['sha256']:
            raise ValueError('UC JAR differs from build inventory')
    dependencies=json.loads((directory/'dependencies.json').read_text())
    covered={d['sha256'] for d in dependencies if d.get('licenses')}
    for jar in report['jars']:
        if not jar['source_name'].startswith('unitycatalog-server-') and jar['sha256'] not in covered:
            raise ValueError('UC runtime dependency lacks license/provenance inventory')
    if digest(directory/'licenses/LICENSE') != pin['inputs']['LICENSE'] or digest(directory/'licenses/NOTICE') != pin['inputs']['NOTICE']:
        raise ValueError('UC source notices differ from reviewed pin')
    return report


def install(destination, target, directory=None):
    directory=directory or ROOT/'build/uc-artifacts'/target
    report=verify(directory,target)
    shutil.copytree(directory,destination/'share/unity-catalog')
    provenance=destination/'provenance/unity-catalog';provenance.mkdir(parents=True)
    for name in ['unity-catalog-source.lock.json','unity-catalog-maven.lock.json']:
        shutil.copy2(ROOT/'components'/name,provenance/name)
    shutil.copy2(directory/'build.json',provenance/'build.json')
    shutil.copy2(directory/'dependencies.json',provenance/'dependencies.json')
    return dict(protocol_version=1,source_commit=report['source_commit'],
        source_pin_sha256=report['source_pin_sha256'],dependency_lock_sha256=report['dependency_lock_sha256'],
        build_sha256=digest(directory/'build.json'),metadata_backend='h2-2.2.224',profile='local-owner-files')
