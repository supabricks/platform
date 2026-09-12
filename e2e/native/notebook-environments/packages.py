"""NE04 exact-release package transactions and clean offline reconstruction."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def qualify(args):
    release = args.release.resolve()
    binary = release / 'bin/supabricks'
    root = Path(tempfile.mkdtemp(prefix='sb-ne04-', dir='/tmp')).resolve()
    root.chmod(0o700)
    data = root / 'data'
    report = dict(status='running', checks=[], release_sha256=digest(release / 'release.json'))
    project = root / 'project'
    project.mkdir()
    env = {k:v for k,v in os.environ.items() if not k.startswith(('UV_', 'PYTHON', 'PIP_', 'JUPYTER', 'IPYTHON'))}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    def cli(*parts, success=True, at=None):
        result = subprocess.run([str(binary), *map(str, parts), '--data-dir', str(data), '--project', str(at or project)], env=env, capture_output=True, text=True, timeout=230)
        (root / 'private-command.log').write_text(result.stdout + result.stderr)
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return json.loads(result.stdout.splitlines()[-1])
    def check(name):
        report['checks'].append(name)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(name, flush=True)
    def api(command, error=False):
        request = dict(version=1, request=dict(method='api',api_version=1,binding=binding,action=dict(action='environment',command=command)))
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(20)
            connection.connect(str(data / 'control.sock'))
            connection.sendall(json.dumps(request).encode()+b'\n')
            response=json.loads(connection.makefile('rb').readline(65536))
        assert ('error' in response) == error, response
        return response.get('result', response.get('error'))
    def operation(*parts, success=True, at=None):
        value=cli('env',*parts,'--wait',success=success,at=at)
        assert value['state'] == ('ready' if success else 'failed'),value
        return value
    def generation(at=None):
        identity=cli('env','status',at=at)['active_generation']
        with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro',uri=True) as db:
            records=[json.loads(r[0]) for r in db.execute('SELECT record_json FROM environment_generations')]
        return next(g for g in records if g['id']==identity)
    def imported(g, package, version):
        code=f"import importlib.metadata as m; import {package}; assert m.version({package!r}) == {version!r}"
        subprocess.run([str(Path(g['path'])/'bin/python'),'-I','-B','-c',code],env=env,check=True)
    started=False
    try:
        subprocess.run([str(binary),'installation','verify'],env=env,check=True,stdout=subprocess.DEVNULL)
        identity=cli('init','ne04')['project']['id']
        binding=dict(project_id=identity,worktree=str(project))
        cli('up');started=True
        operation('init')
        added=operation('add','humanize==4.13.0','--key','add-humanize')
        old=generation(); imported(old,'humanize','4.13.0')
        request=dict(action='manage',key='add-humanize',expected=added['inputs'],change=dict(kind='add',requirement='humanize==4.13.0'),offline=False)
        assert api(request)['id']==added['id']
        assert operation('add','humanize==4.13.0','--key','add-humanize')['id']==added['id']
        api(dict(request,offline=True),error=True)
        check('add_import_and_request_replay_with_conflicting_parameters_rejected')
        operation('add','pandas==1.0.0',success=False)
        assert generation()['id']==old['id']
        check('protected_dependency_conflict_preserves_active_generation')
        operation('add','boltons==24.1.0')
        added_extra=generation();imported(added_extra,'boltons','24.1.0')
        # boltons is deliberately absent from the release: this exercises actual
        # registry wheel acquisition, rather than a bundled fixture/cache hit.
        check('registry_only_user_wheel_download_and_import')
        bundle=root/'environment.zip'
        operation('export-bundle',bundle,'--offline')
        with zipfile.ZipFile(bundle) as archive:
            manifest=json.loads(archive.read('bundle.json'))
            assert manifest['target'] in ('linux-x86_64','macos-arm64')
            assert any('boltons-' in n for n in manifest['files'])
        check('offline_bundle_contains_target_hashes_and_complete_wheels')
        operation('remove','humanize')
        current=generation()
        subprocess.run([str(Path(current['path'])/'bin/python'),'-I','-B','-c',"import importlib.util; assert importlib.util.find_spec('humanize') is None"],env=env,check=True)
        imported(old,'humanize','4.13.0')
        check('remove_builds_replacement_without_mutating_previous_generation')
        # Restore the source-controlled declaration pair onto a new worktree;
        # discard all artifact and resolver caches before offline reconstruction.
        second=root/'second';second.mkdir()
        cli('init','ne04-second',at=second)
        operation('init',at=second)
        with zipfile.ZipFile(bundle) as archive:
            for name in ('pyproject.toml','uv.lock'):
                (second/'notebooks/environment'/name).write_bytes(archive.read(name))
        cli('down');started=False
        for name in ('notebook-environment-cache','notebook-environment-artifacts'):
            shutil.rmtree(data/name)
        cli('up');started=True
        missing=operation('sync','--offline',success=False,at=second)
        assert 'offline wheel artifacts missing' in missing['error'],missing
        # A valid bundle also repairs a corrupted disposable artifact cache.
        boltons_hash=next(v for n,v in manifest['files'].items() if 'boltons-' in n)
        (data/'notebook-environment-artifacts'/boltons_hash).write_bytes(b'interrupted old cache write')
        operation('import-bundle',bundle,at=second)
        assert digest(data/'notebook-environment-artifacts'/boltons_hash)==boltons_hash
        restored=generation(second);imported(restored,'boltons','24.1.0');imported(restored,'humanize','4.13.0')
        operation('sync','--offline',at=second)
        check('clean_offline_missing_artifacts_fail_then_bundle_import_reconstructs_locked_versions')
        hostile=root/'hostile.zip'
        with zipfile.ZipFile(hostile,'w') as archive:
            archive.writestr('../escaped','bad')
        before=generation()['id']
        operation('import-bundle',hostile,success=False)
        assert generation()['id']==before and not (root/'escaped').exists()
        corrupt=root/'corrupt.zip'
        with zipfile.ZipFile(bundle) as src,zipfile.ZipFile(corrupt,'w') as dst:
            for name in src.namelist():
                dst.writestr(name,b'corrupt' if name=='pyproject.toml' else src.read(name))
        operation('import-bundle',corrupt,success=False)
        check('hostile_archive_paths_and_hash_mismatches_cannot_publish')
        lock_path=project/'notebooks/environment/uv.lock'
        original_lock=lock_path.read_text()
        parts=re.split(r'(?=^\[\[package\]\]$)',original_lock,flags=re.M)
        lock_path.write_text(''.join(re.sub(r'sha256:[a-f0-9]{64}','sha256:'+'0'*64,p) if p.startswith('[[package]]\nname = "pyarrow"') else p for p in parts))
        try:
            failure=operation('sync','--offline',success=False)
            assert 'locked wheel hash differs' in failure['error'],failure
            assert generation()['id']==before
        finally:
            lock_path.write_text(original_lock)
        check('bundled_wheels_still_require_matching_committed_lock_hashes')
        source=project/'python-project';source.mkdir()
        for name in ('pyproject.toml','uv.lock'):
            shutil.copyfile(second/'notebooks/environment'/name,source/name)
        operation('adopt','python-project')
        imported(generation(),'humanize','4.13.0')
        check('explicit_project_declaration_adoption_uses_managed_generation')
        # Submit a valid typed mutation, then edit the declaration while the
        # worker is still resolving/building. The daemon must preserve that edit.
        expected=cli('env','status')['inputs']
        pending=api(dict(action='manage',key='external-edit',expected=expected,change=dict(kind='sync'),offline=True))
        lock=project/'notebooks/environment/uv.lock'
        lock.write_text(lock.read_text()+'\n# external editor\n')
        deadline=time.monotonic()+200
        while True:
            state=api(dict(action='status',id=pending['id']))
            if state['state'] in ('ready','failed','cancelled'):break
            assert time.monotonic()<deadline
            time.sleep(.1)
        assert state['state']=='failed',state
        assert lock.read_text().endswith('# external editor\n')
        check('concurrent_external_edit_is_preserved_and_prevents_activation')
        with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro',uri=True) as db:
            assert db.execute('SELECT count(*) FROM analytical_sessions').fetchone()[0]==0
        subprocess.run([str(binary),'installation','verify'],env=env,check=True,stdout=subprocess.DEVNULL)
        check('package_operations_never_acquire_sail_and_service_installation_is_unchanged')
        report['status']='passed'
    except BaseException as error:
        report['status']='failed';report['failure_type']=type(error).__name__
        raise
    finally:
        if started:
            try:cli('down')
            except Exception:report['cleanup_failed']=True;report['status']='failed'
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(dict(status=report['status'],private_workspace=str(root))))
        if report['status']=='passed':shutil.rmtree(root)
    if report.get('cleanup_failed'):raise RuntimeError('NE04 cleanup failed')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    qualify(parser.parse_args())
