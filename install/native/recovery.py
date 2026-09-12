#!/usr/bin/env python3
"""R03 exact-release upgrade, stopped backup/restore and process-failure probes.

No power-loss or OS-reboot claim: the kernel and its caches remain alive.
Every mutation and signal is confined to this harness's fresh private root.
"""
import argparse
import hashlib
from functools import partial
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time

from qualify import Handler, run
from stage import stage


class Checks(list):
    def append(self, message):
        super().append(message)
        print('[R03] ' + message, flush=True)


def qualify(args):
    workspace = Path(tempfile.mkdtemp(prefix='sb-r03-', dir='/tmp')).resolve()
    prefix = workspace / 'programs with spaces'
    data = workspace / 'data'
    project = workspace / 'project'
    project.mkdir()
    env = dict(os.environ, SUPABRICKS_INSTALL_DIR=str(prefix), SUPABRICKS_DATA_DIR=str(data), SUPABRICKS_NO_MODIFY_PATH='1')
    for name in list(env):
        if name.upper().endswith('_PROXY') or name.startswith(('PG', 'AWS_', 'PC_', 'OTEL_')):
            env.pop(name)
    web = workspace / 'web'; web.mkdir()
    for channel, source in [('old', args.previous_directory), ('new', args.directory)]:
        destination = web / channel; destination.mkdir()
        for archive in source.glob('*.tar.gz'):
            shutil.copy2(archive, destination / archive.name)
            checksum = archive.with_name(archive.name + '.sha256')
            shutil.copy2(checksum, destination / checksum.name)
    key = workspace / 'preview.pem'
    run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', key])
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(web)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    stage(web / 'old', args.previous_version, base + '/old', key)
    stage(web / 'new', args.version, base + '/new', key)
    checks = Checks()
    report = dict(status='failed', checks=checks, network_qualification=args.network_evidence,
                  limits=['process-failure recovery; no kernel reboot or power-loss qualification',
                          'same-target physical restore and catalog-9-to-10 upgrade; identical engine/dependency inventories'])
    binary = prefix / 'bin/supabricks'
    roots = [data]
    identify = workspace / 'identify-process.py'
    identify.write_text("""import json, os, sys
import psutil
root, value = sys.argv[1:]
if value == 'daemon':
    matches = [p.info['pid'] for p in psutil.process_iter(['pid','cmdline'])
               if 'daemon' in (p.info['cmdline'] or []) and '--data-dir' in (p.info['cmdline'] or [])
               and root in (p.info['cmdline'] or [])]
    assert len(matches) == 1, 'daemon identity is ambiguous'
    pid = matches[0]
else:
    pid = int(value)
process = psutil.Process(pid)
assert process.uids().effective == os.geteuid(), 'process belongs to another user'
assert root in ' '.join(process.cmdline()), 'process is outside disposable root'
assert process.status() != psutil.STATUS_ZOMBIE, 'process is already a zombie'
print(json.dumps(pid))
""")

    def install(channel, upgrade=False):
        install_env = dict(env)
        if upgrade:
            install_env.update(SUPABRICKS_UPGRADE='1', SUPABRICKS_BACKUP_DIR=str(workspace / 'upgrade-backup'))
        curl = subprocess.Popen(['curl', '-fsSL', base + '/' + channel + '/install.sh'], env=install_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        bash = subprocess.run(['bash'], env=install_env, stdin=curl.stdout, capture_output=True, text=True, timeout=600)
        curl.stdout.close(); curl.wait(timeout=10)
        assert curl.returncode == 0 and bash.returncode == 0, bash.stderr

    def cli(*argv, executable=None, root=None):
        return json.loads(run([executable or binary, *argv, '--data-dir', root or data, '--project', project], env=env, timeout=240))

    def sql(statement, branch='main', write=False):
        return cli('sql', '--branch', branch, '--sql', statement, *(['--write'] if write else []))['rows']

    def analytic_count(branch='main'):
        return cli('analytics', 'sql', '--branch', branch, '--sql', 'SELECT count(*) FROM public.recovery_rows', '--timeout-ms', '30000')['rows']

    def wait_for(fn):
        deadline = time.monotonic() + 180
        last = None
        while time.monotonic() < deadline:
            try:
                result = fn()
                if result: return result
            except (AssertionError, subprocess.TimeoutExpired) as error:
                last = type(error).__name__
            time.sleep(0.5)
        raise AssertionError('recovery convergence timed out: ' + str(last))

    def owned_pid(role):
        assert data.name == 'data' and data.parent.name.startswith('sb-r03-')
        status = cli('status')
        if role == 'daemon':
            pid = 'daemon'
        else:
            records = status['runtime']['processes']
            if role in ('compute', 'postgres'):
                with sqlite3.connect(data / 'state.sqlite3') as db:
                    endpoint = db.execute("SELECT e.id FROM endpoints e JOIN branches b ON b.id=e.branch_id WHERE b.name='main'").fetchone()[0]
                record = next(p for p in records if p['role'] == 'compute-' + endpoint)
                pid = record['pid']
                if role == 'postgres':
                    pid = int((data / 'computes' / endpoint / 'pgdata/postmaster.pid').read_text().splitlines()[0])
                    assert os.getpgid(pid) == record['pid'], 'postmaster outside its owned process group'
            else:
                pid = next(p['pid'] for p in records if p['role'] == role)
        # macOS Seatbelt can refuse execution of the system ps binary. The
        # bundled psutil reads native process identity without weakening sandbox
        # rules or relying on host Python packages.
        return json.loads(run([prefix / 'current/python/analytics/python', identify, data, str(pid)], env=env))

    try:
        install('old')
        old_release = (prefix / 'current').resolve()
        old_identity = json.loads(run([binary, 'installation', 'verify'], env=env))['identity']
        cli('init', 'recovery'); cli('up'); cli('database', 'create', 'main', '--wait')
        sql('CREATE TABLE recovery_rows(id integer PRIMARY KEY, note text)', write=True)
        sql("INSERT INTO recovery_rows VALUES (1,'shared')", write=True)
        cli('branch', 'create', 'experiment', '--from', 'main', '--wait')
        sql("INSERT INTO recovery_rows VALUES (2,'child only')", branch='experiment', write=True)
        sql("INSERT INTO recovery_rows VALUES (3,'parent only')", write=True)
        parent_uri = cli('connect', 'main')['uri']
        branch_uri = cli('connect', 'experiment')['uri']
        epochs = {}
        for branch in ['main', 'experiment']:
            cli('analytics', 'refresh', '--branch', branch, '--wait')
            epochs[branch] = cli('analytics', 'snapshot', '--branch', branch)['publication']['epoch_id']
            assert analytic_count(branch) == [['2']]
        credentials = (data / 'storage.pk8').read_bytes()
        before = {branch: sql('SELECT * FROM recovery_rows ORDER BY id', branch) for branch in epochs}
        checks.append('actual PR34 archive creates acknowledged parent/child data and both analytical epochs')
        install('new', upgrade=True)
        assert (prefix / 'current').resolve() != old_release
        current_release = (prefix / 'current').resolve()
        identity = json.loads(run([binary, 'installation', 'verify'], env=env))['identity']
        saved = json.loads((workspace / 'upgrade-backup/backup.json').read_text())
        assert saved['release']['identity'] == old_identity and saved['consistency'] == 'stopped-cell'
        assert not (data / 'upgrade.json').exists()
        cli('backup', 'verify', workspace / 'upgrade-backup')
        assert saved['schema_version'] == 9
        # Reconstruct exact persisted interruption boundaries while stopped.
        # These exercise the real signed installer/candidate, not a mock migrator.
        completed = json.loads((data / 'last-upgrade.json').read_text())
        journal = dict(version=1, previous=str(old_release), prefix=str(prefix),
                       backup=str(workspace / 'upgrade-backup'),
                       **{'from': completed['from'], 'to': completed['to']},
                       database_sha256=saved['files']['state.sqlite3']['sha256'])
        for phase in ['before_migration', 'after_migration', 'runtime_rebound', 'current_activated']:
            (data / 'upgrade.json').write_text(json.dumps(journal))
            (data / 'upgrade.json').chmod(0o600)
            if phase == 'before_migration':
                shutil.copy2(workspace / 'upgrade-backup/data/state.sqlite3', data / 'state.sqlite3')
            if phase in ('before_migration', 'after_migration'):
                shutil.copy2(workspace / 'upgrade-backup/data/runtime.json', data / 'runtime.json')
            if phase != 'current_activated':
                (prefix / 'current').unlink()
                (prefix / 'current').symlink_to(Path('releases') / args.previous_version)
            blocked = subprocess.run([str(current_release / 'bin/supabricks'), 'up', '--data-dir', str(data)], env=env, capture_output=True, timeout=90)
            assert blocked.returncode != 0, 'pending migration must block startup'
            install('new', upgrade=True)
            with sqlite3.connect(data / 'state.sqlite3') as db:
                assert db.execute('PRAGMA user_version').fetchone()[0] == 10
                assert db.execute('SELECT source_sha256,release_identity FROM catalog_migrations WHERE version=10').fetchone() == (journal['database_sha256'], identity)
            assert not (data / 'upgrade.json').exists()
        before_hash = hashlib.sha256((data / 'state.sqlite3').read_bytes()).hexdigest()
        blocked = subprocess.run([str(old_release / 'bin/supabricks'), 'up', '--data-dir', str(data)], env=env, capture_output=True, timeout=90)
        assert blocked.returncode != 0
        assert hashlib.sha256((data / 'state.sqlite3').read_bytes()).hexdigest() == before_hash
        checks.append('catalog 9-to-10 resumes before/after migration and both activation boundaries; old binary refuses migrated root')
        cli('up')
        for branch in epochs:
            assert sql('SELECT * FROM recovery_rows ORDER BY id', branch) == before[branch]
            assert cli('analytics', 'snapshot', '--branch', branch)['publication']['epoch_id'] == epochs[branch]
            assert analytic_count(branch) == [['2']]
        assert cli('connect', 'main')['uri'] == parent_uri
        assert cli('connect', 'experiment')['uri'] == branch_uri
        assert (data / 'storage.pk8').read_bytes() == credentials
        checks.append(f'signed curl upgrade {args.previous_version} to {args.version} creates verified backup, preserves credentials/URIs, branches and epochs')

        receipt = json.loads(run([current_release / 'python/analytics/python', Path(__file__).with_name('receipt_fixture.py'), binary, data, project], env=env, timeout=600))
        assert receipt['status'] == 'passed'
        checks.extend(receipt['checks'])
        # Restore the expected two-row published fixture after the receipt test.
        cli('analytics', 'refresh', '--branch', 'main', '--wait')

        # Each commit is acknowledged immediately before a targeted SIGKILL.
        # No explicit CHECKPOINT or remote-consistent-LSN wait is inserted.
        for index, role in enumerate(['postgres', 'compute', 'pageserver', 'safekeeper', 'objects', 'supervisor', 'daemon']):
            pid = owned_pid(role)
            row = 100 + index
            sql(f"INSERT INTO recovery_rows VALUES ({row}, 'acknowledged before {role} kill')", write=True)
            os.kill(pid, signal.SIGKILL)
            if role == 'daemon':
                wait_for(lambda: cli('up')['status'] == 'ready')
            expected = 3 + index
            wait_for(lambda: sql('SELECT count(*) FROM recovery_rows') == [[str(expected)]])
            assert sql('SELECT * FROM recovery_rows ORDER BY id', 'experiment') == before['experiment']
            assert analytic_count() == [['2']], 'old analytical epoch changed after runtime recovery'
            checks.append(f'{role} SIGKILL preserves acknowledged writes, child isolation and prior analytical epoch')

        backup = workspace / 'recovery-backup'
        final_rows = sql('SELECT * FROM recovery_rows ORDER BY id')
        cli('analytics', 'refresh', '--branch', 'main', '--wait')
        final_epoch = cli('analytics', 'snapshot', '--branch', 'main')['publication']['epoch_id']
        cli('backup', 'create', backup)
        cli('backup', 'verify', backup)
        with sqlite3.connect(data / 'state.sqlite3') as db:
            assert db.execute('SELECT count(*) FROM native_processes').fetchone()[0] == 0
        restored = workspace / 'restored'; roots.append(restored)
        cli('backup', 'restore', backup, root=restored)
        original = data; data = restored; env['SUPABRICKS_DATA_DIR'] = str(data)
        cli('up')
        assert sql('SELECT * FROM recovery_rows ORDER BY id') == final_rows
        assert sql('SELECT * FROM recovery_rows ORDER BY id', 'experiment') == before['experiment']
        assert cli('analytics', 'snapshot', '--branch', 'main')['publication']['epoch_id'] == final_epoch
        assert analytic_count() == [[str(len(final_rows))]]
        assert cli('connect', 'main')['uri'] == parent_uri
        assert (data / 'storage.pk8').read_bytes() == credentials
        assert (data / 'storage.pk8').stat().st_mode & 0o777 == 0o600
        sql("INSERT INTO recovery_rows VALUES (999,'writable restored parent')", write=True)
        sql("INSERT INTO recovery_rows VALUES (999,'writable restored child')", branch='experiment', write=True)
        cli('down')
        checks.append('coordinated backup restores into new private root with exact data, epochs, credentials and writable parent/child computes')

        # The new recovery command can restore the old backup for old binaries
        # which predate the recovery command. It must not migrate that backup.
        rollback = workspace / 'rollback'; roots.append(rollback)
        cli('backup', 'restore', workspace / 'upgrade-backup', '--release', old_release, root=rollback)
        cli('up', executable=old_release / 'bin/supabricks', root=rollback)
        for branch in epochs:
            got = cli('sql', '--branch', branch, '--sql', 'SELECT * FROM recovery_rows ORDER BY id', executable=old_release / 'bin/supabricks', root=rollback)['rows']
            assert got == before[branch]
        cli('down', executable=old_release / 'bin/supabricks', root=rollback)
        checks.append('catalog-8 backup restores under the exact retained R03 release')

        # Interrupted/corrupt restore never opens an incomplete new root.
        corrupt = backup / 'data/storage.pk8'; saved_key = corrupt.read_bytes(); corrupt.write_bytes(b'corrupt')
        rejected = subprocess.run([str(binary), 'backup', 'restore', str(backup), '--data-dir', str(workspace / 'must-not-exist')], env=env, capture_output=True, timeout=90)
        assert rejected.returncode != 0 and not (workspace / 'must-not-exist').exists()
        corrupt.write_bytes(saved_key)
        cli('backup', 'verify', backup)
        cli('installation', 'uninstall')
        assert not (prefix / 'bin/supabricks').exists()
        assert (data / 'state.sqlite3').exists() and current_release.exists() and old_release.exists()
        checks.append('corrupt backup rejected before destination creation; uninstall retains data and exact versions for recovery')
        report.update(status='passed', release_identity=identity, previous_identity=old_identity, workspace=str(workspace),
                      upgrade_backup_id=saved['id'])
    except BaseException as error:
        report['error'] = str(error)
        # Public reports contain bounded status/journal fields, not raw SQL,
        # credentials, process environments or private engine logs.
        try:
            status = cli('status')
            report['diagnostics'] = dict(generation=status.get('generation'),
                pending_operations=status.get('pending_operations'),
                sql_workers_active=status.get('sql_workers_active'),
                analytical_sessions_active=status.get('analytical_sessions_active'),
                runtime_ready=(status.get('runtime') or {}).get('ready'),
                processes=(status.get('runtime') or {}).get('processes'))
        except Exception as diagnostic_error:
            report['diagnostics'] = dict(status_unavailable=type(diagnostic_error).__name__)
        try:
            with sqlite3.connect(f'file:{data / "state.sqlite3"}?mode=ro', uri=True) as db:
                report['journal'] = [dict(id=row[0], next_step=row[1], step_count=row[2])
                    for row in db.execute("SELECT id,next_step,json_array_length(steps) FROM operations WHERE next_step < json_array_length(steps) LIMIT 50")]
        except Exception as diagnostic_error:
            report['journal_unavailable'] = type(diagnostic_error).__name__
        raise
    finally:
        for root in roots:
            # Use the bound release for each private root; do not scan or signal
            # unrelated installations and do not publish credential-bearing logs.
            if (root / 'runtime.json').exists():
                config = json.loads((root / 'runtime.json').read_text())
                engine = Path(config['bundle'])
                cleanup_binary = engine.parent / 'bin/supabricks'
                if cleanup_binary.exists():
                    try:
                        result = subprocess.run([str(cleanup_binary), 'down', '--data-dir', str(root)], env=env, capture_output=True, timeout=90)
                        if result.returncode:
                            report.setdefault('cleanup_errors', []).append(dict(root=root.name, exit_code=result.returncode))
                    except subprocess.TimeoutExpired:
                        report.setdefault('cleanup_errors', []).append(dict(root=root.name, timeout=True))
        server.shutdown(); server.server_close(); key.unlink(missing_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        if report['status'] == 'passed' and not args.keep:
            shutil.rmtree(workspace)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--previous-directory', required=True, type=Path)
    parser.add_argument('--version', default='v0.1.0-alpha.10')
    parser.add_argument('--previous-version', default='v0.1.0-alpha.8')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--network-evidence', default='not externally isolated')
    parser.add_argument('--keep', action='store_true')
    qualify(parser.parse_args())
