"""NE06 signed installer upgrade and cold recovery using real console kernels.

Run in a loopback-only OS network sandbox. All roots, processes and keys belong
to this fixture. No release bytes, runtime policy or catalog records are patched.
"""
import argparse
from functools import partial
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'install/native'))
from client import Console, execute
from qualify import Handler
from stage import stage


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def allocated(path):
    return sum(p.lstat().st_blocks * 512 for p in path.rglob('*') if p.is_file() and not p.is_symlink())


def qualify(args):
    for name in ('release', 'directory', 'previous_directory', 'bundle_fixture', 'report'):
        setattr(args, name, getattr(args, name).resolve())
    root = Path(tempfile.mkdtemp(prefix='sb-ne06-', dir='/tmp')).resolve()
    root.chmod(0o700)
    prefix = root / "programs ' with spaces"
    data = root / 'data'
    project = root / 'project'; project.mkdir()
    roots = [data]
    channels = []
    report = dict(status='running', checks=[], measurements={},
                  network_evidence=os.environ.get('SB_NE02_NETWORK_EVIDENCE', 'local run; external networking not isolated'))
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'UV_', 'PIP_', 'JUPYTER', 'IPYTHON', 'PG', 'AWS_', 'SUPABRICKS_')) and not k.upper().endswith('_PROXY')}
    # Child processes must not discover host language tools or user caches.
    home = root / 'home'; home.mkdir(mode=0o700)
    env.update(HOME=str(home), PATH='/usr/bin:/bin:/usr/sbin:/sbin', PYTHONDONTWRITEBYTECODE='1',
               SUPABRICKS_INSTALL_DIR=str(prefix), SUPABRICKS_NO_MODIFY_PATH='1')
    binary = prefix / 'bin/supabricks'
    web = root / 'web'; web.mkdir()
    key = root / 'preview.pem'
    server = None

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')

    def check(name):
        report['checks'].append(name); save(); print(name, flush=True)

    def cli(at, *parts, success=True):
        scope = [] if parts[:2] == ('installation', 'verify') else ['--data-dir', str(data), '--project', str(at)]
        result = subprocess.run([str(binary), *map(str, parts), *scope],
                                env=env, capture_output=True, text=True, timeout=240)
        (root / 'private-command.log').write_text(result.stdout + result.stderr)
        assert (result.returncode == 0) == success, 'CLI ' + str(parts[:2]) + ': ' + result.stderr[-2000:]
        return json.loads(result.stdout.splitlines()[-1])

    def operation(*parts, success=True):
        start = time.monotonic()
        value = cli(project, 'env', *parts, '--wait', success=success)
        assert value['state'] == ('ready' if success else 'failed'), value
        report['measurements'].setdefault('operations', []).append(dict(action=str(parts[0]), state=value['state'], seconds=round(time.monotonic()-start, 3)))
        return value

    def records(table):
        assert table in ('environment_generations', 'environment_leases', 'native_processes')
        with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro', uri=True) as db:
            if table == 'environment_leases':
                return list(db.execute('SELECT * FROM environment_leases'))
            return [json.loads(r[0]) for r in db.execute('SELECT record_json FROM ' + table)]

    def generation():
        identity = cli(project, 'env', 'status')['active_generation']
        return next(g for g in records('environment_generations') if g['id'] == identity)

    def prepare(label):
        folder = project / 'notebooks/environment'
        for name in ('pyproject.toml', 'uv.lock'):
            shutil.copyfile(prefix / 'current/python/notebooks/environments' / label / name, folder / name)
        operation('prepare')
        return generation()

    def install(channel, upgrade=False):
        install_env = dict(env, SUPABRICKS_DATA_DIR=str(data))
        if upgrade:
            install_env.update(SUPABRICKS_UPGRADE='1', SUPABRICKS_BACKUP_DIR=str(root / 'upgrade-backup'))
        curl = subprocess.Popen(['curl', '-fsSL', base + '/' + channel + '/install.sh'], env=install_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            result = subprocess.run(['bash'], stdin=curl.stdout, env=install_env, capture_output=True, text=True, timeout=600)
        finally:
            curl.stdout.close()
        curl.wait(timeout=15)
        (root / 'private-installer.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0 and curl.returncode == 0, result.stderr[-2000:]
        return (prefix / 'current').resolve()

    def query(console, execution, version=None, boltons=False):
        ws = console.connect(execution)
        code = "import sys, site\nassert sys.prefix != sys.base_prefix and not site.ENABLE_USER_SITE\nassert spark.table('public.orders').count() == 1\nassert 'before_upgrade' not in globals()\n"
        if version:
            code += f"import humanize\nassert humanize.__version__ == {version!r}\n"
            if not boltons:
                code += "import xxhash\nassert xxhash.xxh32('x').intdigest() > 0\n"
        if boltons:
            code += "import boltons, importlib.metadata as md\nassert md.version('boltons') == '24.1.0'\n"
        assert execute(ws, code + "print('QUALIFIED')").strip() == 'QUALIFIED'
        return ws

    try:
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(key)], check=True, capture_output=True)
        for channel, directory, version in [('old', args.previous_directory, args.previous_version), ('new', args.directory, args.version)]:
            destination = web / channel; destination.mkdir()
            target = json.loads((args.release / 'release.json').read_text())['target']
            archive = directory / f'supabricks-{version}-{target}.tar.gz'
            for path in (archive, Path(str(archive) + '.sha256')):
                shutil.copy2(path, destination / path.name)
            report.setdefault('archives', {})[channel] = dict(version=version, target=target, sha256=digest(archive))
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(web)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        stage(web / 'old', args.previous_version, base + '/old', key)
        stage(web / 'new', args.version, base + '/new', key)
        old_release = install('old')
        old_identity = cli(project, 'installation', 'verify')['identity']
        cli(project, 'init', 'ne06'); cli(project, 'up'); cli(project, 'database', 'create', 'main', '--wait')
        cli(project, 'sql', '--branch', 'main', '--write', '--sql', 'CREATE TABLE public.orders(id int)')
        cli(project, 'sql', '--branch', 'main', '--write', '--sql', 'INSERT INTO public.orders VALUES(1)')
        a = Console(project, cli, channels)
        assert cli(project, 'env', 'status')['declaration']['state'] == 'absent'
        start = time.monotonic(); initial = a.start(); ws = query(a, initial)
        report['measurements']['predecessor_cold_base_start_seconds'] = round(time.monotonic()-start, 3)
        ws.close(); a.stop(initial)
        check('signed_predecessor_install_in_relocated_path_starts_base_notebook_without_host_tools_or_warm_cache')
        old = prepare('fixture-a')
        live = a.start(old['id']); ws = query(a, live, '4.13.0')
        execute(ws, 'before_upgrade = 42')
        epoch = live['epoch_id']
        bundle = root / 'environment.zip'
        operation('export-bundle', bundle, '--offline')
        document = dict(nbformat=4, nbformat_minor=5, metadata={'supabricks': {'binding': {k: live[k] for k in ('branch_id', 'epoch_id', 'environment')}}}, cells=[dict(id='saved', cell_type='code', source="raise RuntimeError('must not replay')", execution_count=1, metadata={'supabricks_outputs': {k: live[k] for k in ('branch_id', 'epoch_id', 'environment')}}, outputs=[dict(output_type='stream', name='stdout', text='retained output\n')])])
        a.request('notebooks/contents', dict(action='save', path='lifecycle.ipynb', document=document, expected_revision=None))
        assert records('environment_leases')
        current = install('new', upgrade=True)
        ws.close()
        identity = cli(project, 'installation', 'verify')['identity']
        assert old_identity != identity and old_release.exists() and current != old_release
        assert digest(current / 'release.json') == digest(args.release / 'release.json')
        assert not records('environment_leases') and not records('native_processes')
        backup = json.loads((root / 'upgrade-backup/backup.json').read_text())
        assert backup['release']['identity'] == old_identity
        cli(project, 'backup', 'verify', root / 'upgrade-backup')
        cli(project, 'up')
        status = cli(project, 'env', 'status')
        assert status['preparation_needed'] and all(not g['compatible'] for g in status['environments'])
        assert Path(old['path']).exists() and Path(old['interpreter']).is_file()
        subprocess.run([str(Path(old['path']) / 'bin/python'), '-I', '-B', '-c', "import humanize; assert humanize.__version__=='4.13.0'"], env=env, check=True)
        a = Console(project, cli, channels)
        assert a.action('list') == []
        assert a.request('notebooks/contents', dict(action='get', path='lifecycle.ipynb'))['value']['document'] == document
        rejected = a.action('create', key=str(uuid.uuid4()), target=a.target, environment=old['id'], epoch=epoch)
        rejected = a.action('start', id=rejected['id'], generation=0, key=str(uuid.uuid4()))
        rejected = a.wait(rejected, 'lost')
        assert 'changed' in rejected['error'] and not records('environment_leases')
        check('upgrade_stops_live_kernel_retains_old_interpreter_and_saved_provenance_but_refuses_old_environment_without_replay')
        operation('sync', '--offline')
        rebuilt = generation()
        assert rebuilt['id'] != old['id'] and rebuilt['installation'] == identity
        live = a.start(rebuilt['id'], epoch); ws = query(a, live, '4.13.0')
        assert live['epoch_id'] == epoch
        prepared = prepare('fixture-b')
        cli(project, 'env', 'gc')
        assert Path(rebuilt['path']).exists(), 'leased previous generation collected'
        assert not Path(old['path']).exists() and old_release.exists(), 'old release must remain retained'
        ws.close(); live = a.restart(live, prepared['id']); ws = query(a, live, '4.14.0')
        assert live['epoch_id'] == epoch
        ws.close(); a.stop(live); cli(project, 'env', 'gc')
        assert not Path(rebuilt['path']).exists()
        check('explicit_rebuild_and_adoption_preserve_epoch_while_gc_respects_live_leases_and_retained_releases')
        # A reviewed bundle has both a target and exact kernel-component identity.
        with zipfile.ZipFile(bundle) as source:
            for field in ('target', 'contract'):
                bad = root / f'wrong-{field}.zip'
                with zipfile.ZipFile(bad, 'w') as destination:
                    for name in source.namelist():
                        contents = source.read(name)
                        if name == 'bundle.json':
                            manifest = json.loads(contents); manifest[field] = 'incompatible'
                            contents = json.dumps(manifest).encode()
                        destination.writestr(name, contents)
                operation('import-bundle', bad, success=False)
                assert generation()['id'] == prepared['id']
        check('cross_target_and_incompatible_component_bundles_fail_without_changing_active_environment')
        cli(project, 'down'); cli(project, 'up')
        assert generation()['id'] == prepared['id'] and not cli(project, 'env', 'status')['preparation_needed']
        a = Console(project, cli, channels); assert a.action('list') == []
        live = a.start(prepared['id'], epoch); ws = query(a, live, '4.14.0'); ws.close(); a.stop(live)
        check('down_up_retains_prepared_environment_and_restarts_only_on_explicit_request')
        # Export under this exact release, then back up only durable platform data.
        operation('import-bundle', args.bundle_fixture)
        final_bundle = root / 'restoration.zip'; operation('export-bundle', final_bundle, '--offline')
        final_generation = generation()
        report['measurements'].update(generation_allocated_bytes=allocated(Path(final_generation['path'])), bundle_bytes=final_bundle.stat().st_size)
        final_backup = root / 'backup'; cli(project, 'backup', 'create', final_backup); cli(project, 'backup', 'verify', final_backup)
        saved = json.loads((final_backup / 'backup.json').read_text())
        assert not any(name.startswith(('notebook-environments/', 'notebook-environment-cache/', 'notebook-environment-artifacts/')) for name in saved['files'])
        restored = root / 'restored data'; roots.append(restored)
        original_data = data
        data = restored
        cli(project, 'backup', 'restore', final_backup)
        moved = root / 'moved project'; shutil.move(project, moved); project = moved
        cli(project, 'up')
        status = cli(project, 'env', 'status')
        assert status['preparation_needed'] and not status['environments']
        assert all(g['state'] != 'ready' for g in records('environment_generations'))
        assert Path(final_generation['path']).exists(), 'restore touched the original data root'
        assert not (data / 'notebook-environment-cache').exists() or not any((data / 'notebook-environment-cache').iterdir())
        missing = operation('sync', '--offline', success=False)
        assert 'offline wheel artifacts missing' in missing['error']
        restored_operation = operation('import-bundle', final_bundle)
        with zipfile.ZipFile(final_bundle) as archive:
            report['project_bundle'] = dict(sha256=digest(final_bundle), manifest=json.loads(archive.read('bundle.json')), packages=restored_operation['result']['packages'])
        restored_generation = generation()
        assert Path(restored_generation['path']).is_relative_to(data)
        assert restored_generation['id'] != final_generation['id'] and restored_generation['worktree'] == str(project)
        a = Console(project, cli, channels)
        assert a.action('list') == []
        assert a.request('notebooks/contents', dict(action='get', path='lifecycle.ipynb'))['value']['document'] == document
        live = a.start(restored_generation['id'], epoch); ws = query(a, live, '4.13.0', boltons=True); ws.close(); a.stop(live)
        cli(project, 'env', 'gc')
        assert Path(final_generation['path']).exists() and (original_data / 'state.sqlite3').exists()
        check('cold_backup_restore_and_project_move_rebuild_from_explicit_bundle_and_execute_preserved_snapshot_without_cross_root_reuse')
        manifest = json.loads((current / 'release.json').read_text())
        contract = json.loads((current / 'python/notebooks/kernel-contract.json').read_text())
        provenance = json.loads((current / 'provenance/environment-build.json').read_text())
        report.update(release_identity=identity, previous_identity=old_identity, release_sha256=digest(current / 'release.json'),
                      source=manifest['provenance'], python_version=contract['python_version'], target=contract['target'],
                      kernel_contract_sha256=digest(current / 'python/notebooks/kernel-contract.json'),
                      wheels=provenance['wheels'], notices={n: v['sha256'] for n, v in manifest['files'].items() if n.startswith('licenses/')})
        cli(project, 'installation', 'verify')
        check('final_release_source_pins_wheel_hashes_notices_and_measured_costs_recorded')
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', failure_type=type(error).__name__)
        raise
    finally:
        for ws in channels:
            ws.close()
        for data_root in roots:
            if (data_root / 'runtime.json').exists():
                config = json.loads((data_root / 'runtime.json').read_text())
                cleanup_binary = Path(config['bundle']).parent / 'bin/supabricks'
                try:
                    result = subprocess.run([str(cleanup_binary), 'down', '--data-dir', str(data_root)], env=env, capture_output=True, timeout=120)
                    if result.returncode: raise RuntimeError('shutdown failed')
                except Exception:
                    report['cleanup_failed'] = True; report['status'] = 'failed'
        if server:
            server.shutdown(); server.server_close()
        key.unlink(missing_ok=True)
        save()
        print(json.dumps(dict(status=report['status'], private_workspace=str(root))), flush=True)
        if report['status'] == 'passed': shutil.rmtree(root)
    if report.get('cleanup_failed'): raise RuntimeError('NE06 cleanup failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('release', 'directory', 'previous-directory', 'report', 'bundle-fixture'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--previous-version', required=True)
    qualify(parser.parse_args())
