"""Explicit platform-only upgrade controls; never a distributable engine release.

The real candidate must first reject an incompatible historical engine. Positive
platform/catalog migration checks use the identical candidate platform payload
with the predecessor engine retained. Both identities are recorded separately.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def engine_files(manifest):
    return {name:entry for name,entry in manifest['files'].items() if name.startswith('engine/')}


def archive_engine_differs(archive, previous):
    with tarfile.open(archive) as tar:
        manifest=json.load(tar.extractfile('supabricks/release.json'))
    old=json.loads((previous/'release.json').read_text())
    return engine_files(manifest)!=engine_files(old)


def checkpoint(prefix, data):
    # Call only after stopping the source runtime, before attempting the upgrade.
    return dict(current=os.readlink(prefix/'current'),
                state=digest(data/'state.sqlite3'),runtime=digest(data/'runtime.json'))


def assert_refused(result, prefix, data, backup, before):
    assert result.returncode!=0, 'incompatible historical engine upgrade unexpectedly succeeded'
    assert 'incompatible engine, library, analytical or data format' in result.stderr, result.stderr[-1500:]
    assert checkpoint(prefix,data)==before, 'rejected upgrade changed source state or active release'
    assert not backup.exists() and not (data/'upgrade.json').exists(), 'rejected upgrade began mutation'


def control_manifest(candidate, previous):
    source=json.loads((candidate/'release.json').read_text())
    old=json.loads((previous/'release.json').read_text())
    assert source['target']==old['target'] and source['profile']==old['profile']
    assert engine_files(source)!=engine_files(old), 'control unnecessary for identical engines'
    assert 'engine/manifest.json' in engine_files(old)
    source['version'] += '.platformcontrol'
    receipt=dict(kind='platform-only-upgrade-control',
        candidate_identity=digest(candidate/'release.json'),
        predecessor_identity=digest(previous/'release.json'),
        scope='candidate platform and catalog with exact predecessor engine; not an in-place engine upgrade or distributed candidate archive')
    source['provenance']['qualification_control']=receipt
    source['files']={name:entry for name,entry in source['files'].items() if not name.startswith('engine/')}
    source['files'].update(engine_files(old))
    source['provenance']['engine_manifest_sha256']=digest(previous/'engine/manifest.json')
    encoded=(json.dumps(source,indent=2)+'\n').encode()
    receipt=dict(receipt,control_identity=hashlib.sha256(encoded).hexdigest(),version=source['version'])
    return source,encoded,receipt


def create(candidate, previous, destination):
    _,encoded,receipt=control_manifest(candidate,previous)
    shutil.copytree(candidate,destination,symlinks=True)
    shutil.rmtree(destination/'engine')
    shutil.copytree(previous/'engine',destination/'engine',symlinks=True)
    (destination/'release.json').write_bytes(encoded)
    return receipt


def verify(release):
    subprocess.run([str(release/'bin/supabricks'),'installation','verify'],check=True,capture_output=True,timeout=120)


def stage_control(candidate, previous, directory, base_url, key):
    from stage import stage
    directory.mkdir()
    manifest,encoded,receipt=control_manifest(candidate,previous)
    archive=directory/f"supabricks-{receipt['version']}-{manifest['target']}.tar.gz"
    # Stream verified inputs directly: no third unpacked multi-GB release.
    with tarfile.open(archive,'w:gz',compresslevel=1,format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(manifest['files']):
            source=(previous if name.startswith('engine/') else candidate)/name
            assert source.is_file() and not source.is_symlink(), name
            assert digest(source)==manifest['files'][name]['sha256'], name
            tar.add(source,arcname='supabricks/'+name,recursive=False)
        info=tarfile.TarInfo('supabricks/release.json');info.size=len(encoded);info.mode=0o644
        tar.addfile(info,io.BytesIO(encoded))
    Path(str(archive)+'.sha256').write_text(f'{digest(archive)}  {archive.name}\n')
    stage(directory,receipt['version'],base_url,key)
    return receipt
