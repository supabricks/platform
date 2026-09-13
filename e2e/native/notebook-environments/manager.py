"""NE02 exact-release qualification; fault injection only in this private fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import psutil


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def qualify(args):
    release = args.release.resolve()
    binary = release / 'bin/supabricks'
    root = Path(tempfile.mkdtemp(prefix='sb-ne02-', dir='/tmp')).resolve()
    root.chmod(0o700)
    data = root / 'data'
    report = dict(status='running', checks=[], release_sha256=digest(release / 'release.json'),
                  network_evidence=os.environ.get('SB_NE02_NETWORK_EVIDENCE', 'local run; external networking not isolated'))
    children = []
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'UV_', 'PIP_', 'JUPYTER', 'IPYTHON', 'PG', 'AWS_'))}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    def cli(project, *parts):
        p = subprocess.run([str(binary), *map(str, parts), '--data-dir', str(data), '--project', str(project)],
                           env=env, capture_output=True, text=True, timeout=180)
        if p.returncode:
            (root / 'private-command.log').write_text(p.stdout + p.stderr)
            raise RuntimeError('NE02 CLI failed: ' + str(parts[0]))
        return json.loads(p.stdout.splitlines()[-1])
    def check(name):
        report['checks'].append(name)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(name, flush=True)
    def wait(fn, seconds=90):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = fn()
            if value:
                return value
            time.sleep(.02)
        raise TimeoutError('NE02 convergence')
    def records(table):
        assert table in ['native_processes', 'environment_operations', 'environment_generations']
        with sqlite3.connect(f'file:{data}/state.sqlite3?mode=ro', uri=True) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT record_json FROM ' + table)]
    def api(binding, command, error=False):
        envelope = dict(version=1, request=dict(method='api', api_version=1, binding=binding,
                        action=dict(action='environment', command=command)))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15)
            client.connect(str(data / 'control.sock'))
            client.sendall(json.dumps(envelope).encode() + b'\n')
            value = json.loads(client.makefile('rb').readline(65536))
        assert ('error' in value) == error, value
        return value.get('result', value.get('error'))
    def command(binding, action, **fields):
        return api(binding, dict(action=action, **fields))
    def settled(binding, operation, state='ready'):
        def poll():
            value = command(binding, 'status', id=operation['id'])
            return value if value['state'] in ['ready', 'failed', 'cancelled'] else None
        result = wait(poll)
        if result['state'] != state:
            (root / 'private-operation.json').write_text(json.dumps(result))
            raise AssertionError('NE02 operation expected ' + state + ', got ' + result['state'] + ': ' + str(result['error']))
        return result
    def prepare(binding, key):
        expected = command(binding, 'inspect')['inputs']
        return command(binding, 'prepare', key=key, expected=expected)
    def freeze(operation):
        def find():
            return next((p for p in records('native_processes') if p['role'] == 'environment-' + operation['id']), None)
        record = wait(find, 20)
        process = psutil.Process(record['pid'])
        assert process.uids().effective == os.getuid()
        assert process.environ()['SUPABRICKS_PROCESS_TOKEN'] == record['token']
        # Verify every member before stopping this fixture's isolated group.
        for p in psutil.process_iter(['pid']):
            try:
                if os.getpgid(p.pid) == record['pid']:
                    assert p.environ()['SUPABRICKS_PROCESS_TOKEN'] == record['token']
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
        os.killpg(record['pid'], signal.SIGSTOP)
        return record
    projects = []
    try:
        subprocess.run([str(binary), 'installation', 'verify'], env=env, check=True, stdout=subprocess.DEVNULL)
        for label in ['a', 'b']:
            project = root / ('project-' + label)
            project.mkdir()
            identity = cli(project, 'init', 'ne02-' + label)['project']['id']
            projects.append(dict(project_id=identity, worktree=str(project)))
        a, b = projects
        cli(Path(a['worktree']), 'up')
        for binding, template in [(a, 'base'), (b, 'fixture-b')]:
            request = dict(action='initialize', key='init', template=template)
            operation = api(binding, request)
            settled(binding, operation)
            assert api(binding, request)['id'] == operation['id']
        check('atomic_initialization_and_idempotent_reconnect')
        one, two = prepare(a, 'first'), prepare(b, 'first')
        api(a, dict(action='prepare', key='racing', expected=one['inputs']), error=True)
        def pair():
            assert len([p for p in records('native_processes') if p['role'].startswith('environment-')]) <= 1
            states = [command(binding, 'status', id=o['id'])['state'] for binding, o in [(a, one), (b, two)]]
            assert not any(s in ['failed', 'cancelled'] for s in states), states
            return all(s == 'ready' for s in states)
        wait(pair, 150)
        check('two_worktrees_serialize_preparation_and_same_worktree_conflicts')
        old = command(a, 'inspect')['active_generation']
        for binding, expected in [(a, None), (b, '4.14.0')]:
            state = command(binding, 'inspect')
            assert not state['preparation_needed']
            g = next(g for g in records('environment_generations') if g['id'] == state['active_generation'])
            code = "import json,sys,importlib.metadata as m;print(json.dumps(dict(prefix=sys.prefix,humanize=next((d.version for d in m.distributions() if d.metadata['Name']=='humanize'),None))))"
            result = json.loads(subprocess.check_output([str(Path(g['path']) / 'bin/python'), '-I', '-B', '-c', code], env=env))
            assert result['prefix'] == g['path'] and result['humanize'] == expected
        check('actual_offline_base_and_fixture_venvs_have_exact_packages')
        unrelated = subprocess.Popen(['/bin/sleep', '600'], start_new_session=True)
        children.append(unrelated)
        operation = prepare(a, 'cancel')
        owned = freeze(operation)
        started = time.monotonic()
        command(a, 'cancel', id=operation['id'])
        settled(a, operation, 'cancelled')
        assert time.monotonic() - started < 5
        assert not psutil.pid_exists(owned['pid']) and unrelated.poll() is None
        assert command(a, 'inspect')['active_generation'] == old
        check('in_flight_cancellation_reaps_only_owned_processes_within_five_seconds')
        operation = prepare(a, 'stale')
        owned = freeze(operation)
        lock = Path(a['worktree']) / 'notebooks/environment/uv.lock'
        original = lock.read_bytes()
        lock.write_bytes(original + b'\n# changed during prepare\n')
        os.killpg(owned['pid'], signal.SIGCONT)
        settled(a, operation, 'failed')
        assert command(a, 'inspect')['active_generation'] == old
        lock.write_bytes(original)
        check('stale_inputs_never_publish_over_the_previous_ready_generation')
        # Substitute a project declaration. No write may follow it.
        external = root / 'external.lock'
        external.write_bytes(original)
        expected = command(a, 'inspect')['inputs']
        lock.unlink()
        lock.symlink_to(external)
        inspection = command(a, 'inspect')
        assert inspection['declaration']['state'] == 'invalid' and inspection['declaration']['error']
        assert inspection['inputs'] is None and inspection['preparation_needed']
        assert inspection['active_generation'] == old
        api(a, dict(action='prepare', key='symlink-refusal', expected=expected), error=True)
        assert external.read_bytes() == original
        lock.unlink()
        lock.write_bytes(original)
        check('symlink_declarations_are_rejected_without_touching_targets')
        # Exercise hash failure after the release has been verified at startup.
        # Restore bytes before restarting or verifying the installation again.
        wheel = next((release / 'python/notebooks/wheelhouse').glob('humanize-4.14.0-*.whl'))
        contents = wheel.read_bytes()
        try:
            wheel.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
            settled(a, prepare(a, 'corrupt'), 'failed')
            assert command(a, 'inspect')['active_generation'] == old
        finally:
            wheel.write_bytes(contents)
        check('corrupt_bundled_artifact_fails_without_replacing_active_environment')
        operation = prepare(a, 'crash')
        owned = freeze(operation)
        daemon = [p for p in psutil.process_iter(['cmdline']) if 'daemon' in (p.info['cmdline'] or []) and str(data) in (p.info['cmdline'] or [])]
        assert len(daemon) == 1 and daemon[0].uids().effective == os.getuid()
        daemon[0].kill()
        def daemon_dead():
            try:
                return not daemon[0].is_running() or daemon[0].status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True
        wait(daemon_dead, 10)
        cli(Path(a['worktree']), 'up')
        settled(a, operation, 'failed')
        assert not psutil.pid_exists(owned['pid']) and unrelated.poll() is None
        assert command(a, 'prepare', key='crash', expected=operation['inputs'])['id'] == operation['id']
        assert command(a, 'inspect')['active_generation'] == old
        settled(a, prepare(a, 'after-crash'))
        check('daemon_crash_recovery_preserves_active_generation_and_request_identity')
        invalid = next(g for g in records('environment_generations') if g['state'] == 'invalid' and Path(g['path']).is_dir())
        path = Path(invalid['path'])
        held = path.with_suffix('.held')
        path.rename(held)
        target = root / 'unrelated-directory'
        target.mkdir()
        (target / 'keep').write_text('unrelated')
        path.symlink_to(target)
        command(a, 'collect')
        assert (target / 'keep').read_text() == 'unrelated' and held.exists()
        path.unlink()
        held.rename(path)
        command(a, 'collect')
        assert not path.exists()
        check('collection_quarantines_substituted_roots_and_removes_only_known_generations')
        before = command(a, 'inspect')['active_generation']
        cli(Path(a['worktree']), 'down')
        cli(Path(a['worktree']), 'up')
        assert command(a, 'inspect')['active_generation'] == before
        assert not command(a, 'inspect')['preparation_needed']
        subprocess.run([str(binary), 'installation', 'verify'], env=env, check=True, stdout=subprocess.DEVNULL)
        check('clean_restart_retains_ready_environment_and_service_inventory_is_unchanged')
        report['status'] = 'passed'
    except BaseException as error:
        report['status'] = 'failed'
        report['failure_type'] = type(error).__name__
        raise
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        if projects:
            try:
                cli(Path(projects[0]['worktree']), 'down')
            except Exception:
                report['cleanup_failed'] = True
                report['status'] = 'failed'
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'status': report['status'], 'private_workspace': str(root)}))
        if report['status'] == 'passed':
            shutil.rmtree(root)
    if report.get('cleanup_failed'):
        raise RuntimeError('NE02 cleanup failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    qualify(parser.parse_args())
