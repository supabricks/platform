#!/usr/bin/env python3
"""NE01 offline qualification using the real daemon, child gate and Jupyter server.

Only this harness's derivative wrapper selects an interpreter. No production
environment-selection API, kernel patch, fake gate or package-install API exists.
"""
import argparse
import asyncio
import hashlib
import http.cookiejar
import http.server
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
import urllib.request
import uuid

import psutil
from jupyter_server.services.kernels.connection.base import serialize_msg_to_ws_v1, deserialize_msg_from_ws_v1
from tornado.httpclient import HTTPRequest
from tornado.websocket import websocket_connect


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    return {str(p.relative_to(root)): digest(p) for p in root.rglob('*')
            if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts}


def size(root):
    files = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
    return {'logical_bytes': sum(p.stat().st_size for p in files),
            'allocated_bytes': sum(p.stat().st_blocks * 512 for p in files)}


class Probe:
    def __init__(self, args):
        self.args = args
        self.bundle = args.probe.resolve()
        self.service = self.bundle / 'service'
        self.python = self.service / 'python/runtime/bin/python3.12'
        self.uv = self.bundle / 'bin/uv'
        self.root = Path(tempfile.mkdtemp(prefix='sb-ne01-', dir='/tmp')).resolve()
        self.root.chmod(0o700)
        (self.root / 'home').mkdir()
        (self.root / 'empty-bin').mkdir()
        # No host Python, uv, user configuration or warm cache is discoverable.
        self.env = dict(HOME=str(self.root / 'home'), PATH=str(self.root / 'empty-bin'),
                        LANG='C.UTF-8', UV_CACHE_DIR=str(self.root / 'cache'),
                        UV_PYTHON_DOWNLOADS='never', PYTHONDONTWRITEBYTECODE='1',
                        OTEL_SDK_DISABLED='true')
        self.report = {'status': 'running', 'checks': [], 'target': args.target,
                       'network_evidence': os.environ.get('SB_NE01_NETWORK_EVIDENCE', 'local run; external network not isolated')}
        self.projects = []

    def check(self, name, **evidence):
        self.report['checks'].append({'name': name, **evidence})
        self.save()
        print(name, flush=True)

    def save(self):
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.args.report.write_text(json.dumps(self.report, indent=2) + '\n')

    def run(self, command, *, success=True, timeout=120, **kwargs):
        p = subprocess.run(list(map(str, command)), env=self.env, cwd=self.root,
                           capture_output=True, text=True, timeout=timeout, **kwargs)
        if (p.returncode == 0) != success:
            # Full diagnostics remain private; reports contain no launch tokens.
            (self.root / 'last-command.log').write_text(p.stdout + p.stderr)
            raise RuntimeError('unexpected command result: ' + str(command[0]))
        return p

    def uv_command(self, *parts):
        return [self.uv, '--no-config', '--offline', '--no-python-downloads', *parts]

    def sync_command(self, environment, lock):
        return self.uv_command('pip', 'sync', '--python', environment / 'bin/python',
                               '--no-index', '--find-links', self.bundle / 'kernel-wheels',
                               '--only-binary', ':all:', '--require-hashes', '--link-mode', 'copy', lock)

    def prepare(self, label, lock):
        environment = self.root / 'environments' / label
        started = time.monotonic()
        self.run(self.uv_command('venv', '--python', self.python, environment))
        self.run(self.sync_command(environment, lock))
        self.run(self.uv_command('pip', 'check', '--python', environment / 'bin/python'))
        config = (environment / 'pyvenv.cfg').read_text()
        assert 'include-system-site-packages = false' in config
        self.check('prepare_' + label, seconds=time.monotonic() - started, **size(environment))
        return environment

    def python_json(self, python, code):
        return json.loads(self.run([python, '-I', '-B', '-c', code]).stdout)

    def environment_tests(self):
        assert not (self.root / 'cache').exists()
        assert shutil.which('uv', path=self.env['PATH']) is None
        assert shutil.which('python', path=self.env['PATH']) is None
        manifest = json.loads((self.bundle / 'probe.json').read_text())
        for path, expected in manifest['files'].items():
            assert digest(self.bundle / path) == expected
        self.report['package'] = {k: manifest[k] for k in ['wheel_bytes', 'uv_bytes', 'notebook_lock_sha256']}
        self.report['package']['base_distributions'] = len(manifest['packages'])
        self.service_before = inventory(self.service / 'python')
        base = self.prepare('base-cold', self.bundle / 'base.lock')
        warm = self.prepare('base-warm', self.bundle / 'base.lock')
        self.a = self.prepare('project-a', self.bundle / 'a.lock')
        self.b = self.prepare('project-b', self.bundle / 'b.lock')
        for env in [base, warm, self.a, self.b]:
            state = self.python_json(env / 'bin/python',
                'import json,sys,site,importlib.metadata as m; print(json.dumps(dict(prefix=sys.prefix,base=sys.base_prefix,'
                'user=site.ENABLE_USER_SITE,packages={d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions()})))')
            assert Path(state['prefix']) == env and Path(state['base']) == self.python.parent.parent
            assert not state['user']
            expected = {n: p['version'] for n, p in manifest['packages'].items()}
            if env in [self.a, self.b]:
                expected.update(humanize='4.13.0' if env == self.a else '4.14.0', xxhash='3.5.0')
            assert state['packages'] == expected, state['packages']
        self.check('offline_exact_base_and_two_distinct_project_inventories_no_system_site')
        # Explicit unsupported target wheel must fail before any distribution is added.
        unsupported = self.root / 'unsupported'
        unsupported.mkdir()
        original = next((self.bundle / 'kernel-wheels').glob('xxhash-*.whl'))
        wrong = unsupported / 'xxhash-3.5.0-cp312-cp312-win_amd64.whl'
        shutil.copy2(original, wrong)
        before = inventory(base)
        p = self.run(self.uv_command('pip', 'install', '--python', base / 'bin/python',
                     '--no-index', '--only-binary', ':all:', wrong), success=False)
        assert 'compatible' in p.stderr.lower() or 'platform' in p.stderr.lower()
        assert inventory(base) == before
        self.check('unsupported_wheel_rejected_without_mutation')
        p = self.run(self.uv_command('pip', 'install', '--python', base / 'bin/python',
                     '--no-index', '--find-links', self.bundle / 'kernel-wheels', '--only-binary', ':all:',
                     '--constraint', (self.bundle / 'protected.txt').as_uri(), 'ipykernel==0.0.0'), success=False)
        assert 'ipykernel' in p.stderr and ('unsatisfiable' in p.stderr or 'No solution' in p.stderr)
        assert inventory(base) == before
        self.check('protected_contract_conflict_rejected_without_mutation')
        # Copy mode must protect both a warm uv cache and every other environment.
        cache_before = inventory(self.root / 'cache')
        other_before = inventory(self.b)
        source = self.a / 'lib/python3.12/site-packages/humanize/__init__.py'
        old = source.read_bytes()
        source.write_bytes(old + b'\nNE01_MUTATION = True\n')
        assert inventory(self.root / 'cache') == cache_before
        assert inventory(self.b) == other_before
        assert inventory(self.service / 'python') == self.service_before
        source.write_bytes(old)
        self.check('package_mutation_does_not_reach_service_other_project_or_cache')
        # Venv entry points retain absolute paths: move declarations and rebuild.
        script = base / 'bin/ipython'
        assert str(base) in script.read_text().splitlines()[0] or str(base) in script.read_text()[:512]
        moved = base.with_name('moved-venv')
        base.rename(moved)
        try:
            try:
                self.run([moved / 'bin/ipython', '--version'], success=False)
            except FileNotFoundError:
                pass
        finally:
            moved.rename(base)
        # Bundled Python can relocate before creation, but existing venv links
        # cannot survive the interpreter disappearing at their recorded path.
        moved_service = self.service.with_name('moved-service')
        self.service.rename(moved_service)
        try:
            assert not (base / 'bin/python').exists()
        finally:
            moved_service.rename(self.service)
        self.run([base / 'bin/python', '-I', '-B', '-c', 'import ipykernel,pyarrow'])
        self.check('release_relocated_before_creation_venv_and_interpreter_move_require_rebuild')
        self.cancel_test(base)
        self.report['cache'] = size(self.root / 'cache')
        shutil.rmtree(base)
        shutil.rmtree(warm)

    def cancel_test(self, environment):
        entered, release = threading.Event(), threading.Event()
        class SlowIndex(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set()
                release.wait(15)
                self.send_response(404)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SlowIndex)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        before = inventory(environment)
        command = [self.uv, '--no-config', '--no-python-downloads', 'pip', 'install',
                   '--python', environment / 'bin/python', '--only-binary', ':all:', '--link-mode', 'copy',
                   '--index-url', f'http://127.0.0.1:{server.server_port}/simple', 'ne01-cancellation-fixture==1.0']
        child = subprocess.Popen(list(map(str, command)), env=self.env, cwd=self.root,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            assert entered.wait(10), 'uv never reached the controlled loopback index'
            started = time.monotonic()
            os.killpg(child.pid, signal.SIGTERM)
            assert child.wait(timeout=5) != 0
            assert inventory(environment) == before
            self.run(self.sync_command(environment, self.bundle / 'base.lock'))
            self.check('cancel_in_flight_uv_then_retry', seconds=time.monotonic() - started)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()

    def cli(self, project, *parts):
        p = self.run([self.service / 'bin/supabricks', *parts, '--project', project,
                      '--data-dir', self.root / 'data'], timeout=180)
        return json.loads(p.stdout.splitlines()[-1])

    def api(self, path, data=None):
        request = urllib.request.Request(self.origin + path, headers=self.headers,
                    data=None if data is None else json.dumps(data).encode())
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)

    def action(self, **command):
        return self.api('/api/workspace', {'action': 'notebook', 'command': command})['value']

    async def state(self, entry, states, seconds=120):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            entry = self.action(action='status', id=entry['id'], generation=entry['generation'])
            if entry['state'] in states:
                return entry
            if entry['state'] in ['failed', 'lost', 'expired']:
                raise RuntimeError('kernel: ' + str(entry.get('error')))
            await asyncio.sleep(.15)
        raise TimeoutError('notebook state')

    async def connect(self, entry):
        ticket = self.api('/api/notebooks/ticket', {'id': entry['id'], 'generation': entry['generation']})
        cookie = '; '.join(c.name + '=' + c.value for c in self.cookies)
        request = HTTPRequest(self.origin.replace('http:', 'ws:') +
            f"/api/notebooks/{entry['id']}/{entry['generation']}/channels",
            headers={'Origin': self.origin, 'Cookie': cookie})
        return await websocket_connect(request, subprotocols=[ticket['protocol'], ticket['authorization_protocol']],
                                       max_message_size=2 * 1024 * 1024)

    async def execute(self, ws, code, started=None):
        identity = str(uuid.uuid4())
        message = {'header': {'msg_id': identity, 'session': str(uuid.uuid4()), 'username': 'ne01',
                   'date': '2026-09-10T00:00:00Z', 'msg_type': 'execute_request', 'version': '5.3'},
                   'parent_header': {}, 'metadata': {}, 'content': {'code': code, 'silent': False,
                   'store_history': True, 'user_expressions': {}, 'allow_stdin': False, 'stop_on_error': True}}
        await ws.write_message(serialize_msg_to_ws_v1(message, 'shell', pack=lambda v: json.dumps(v).encode()), binary=True)
        reply, idle, outputs = None, False, []
        async with asyncio.timeout(60):
            while not (reply and idle):
                incoming = await ws.read_message()
                if incoming is None:
                    raise RuntimeError('kernel channel closed')
                _, parts = deserialize_msg_from_ws_v1(incoming)
                value = dict(zip(['header', 'parent_header', 'metadata', 'content'], map(json.loads, parts[:4])))
                if value['parent_header'].get('msg_id') != identity:
                    continue
                kind, content = value['header']['msg_type'], value['content']
                if kind == 'execute_reply':
                    reply = content
                elif kind == 'status' and content['execution_state'] == 'idle':
                    idle = True
                elif kind == 'stream':
                    outputs.append(content['text'])
                    if started and 'NE01_BUSY' in content['text']:
                        started.set()
        return reply, ''.join(outputs)

    def processes(self):
        with sqlite3.connect(f'file:{self.root}/data/state.sqlite3?mode=ro', uri=True) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record_json FROM native_processes')]

    async def product_tests(self):
        for label, environment in [('a', self.a), ('b', self.b)]:
            project = self.root / ('project-' + label)
            project.mkdir()
            self.projects.append(project)
            (project / '.ne01-python').write_text(str(environment / 'bin/python') + '\n')
            self.cli(project, 'init', 'ne01-' + label)
            self.cli(project, 'up')
            self.cli(project, 'database', 'create', 'main', '--wait')
            self.cli(project, 'sql', '--branch', 'main', '--write', '--sql',
                     'CREATE TABLE public.orders(id int,amount numeric(18,2))')
            self.cli(project, 'sql', '--branch', 'main', '--write', '--sql',
                     'INSERT INTO public.orders VALUES(1,12.50),(2,7.25)')
            url = self.cli(project, 'console', '--no-open')['url']
            self.origin = url.split('/#')[0].rstrip('/')
            self.cookies = http.cookiejar.CookieJar()
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies))
            self.headers = {'Origin': self.origin, 'X-Supabricks-Console': '1', 'Content-Type': 'application/json'}
            self.headers['X-Supabricks-CSRF'] = self.api('/api/session', {'token': url.split('#launch=')[1]})['csrf']
            branch = next(b for b in self.api('/api/overview')['branches'] if b['name'] == 'main')
            entry = self.action(action='create', key='ne01', target={'branch': branch['id'], 'revision': branch['revision']})
            entry = self.action(action='start', id=entry['id'], generation=0, key='start')
            entry = await self.state(entry, ['ready'])
            epoch = entry['epoch_id']
            ws = await self.connect(entry)
            try:
                reply, output = await self.execute(ws, """import json,sys,importlib.metadata as md
import ipykernel, pyarrow, pandas, humanize, xxhash, psutil
assert spark.table('public.orders').count() == 2
assert str(spark.sql('SELECT sum(amount) AS total FROM public.orders').first().total) == '19.75'
assert humanize.intcomma(1234) == '1,234'
assert len(xxhash.xxh64(b'Supabricks').hexdigest()) == 16
assert type(get_ipython().kernel.session).__name__ == 'BoundedSession'
print(json.dumps(dict(prefix=sys.prefix,base=sys.base_prefix,executable=sys.executable,
    humanize=md.version('humanize'),native=xxhash._xxhash.__file__,kernel=ipykernel.__file__,
    arrow=pyarrow.__file__,rss=psutil.Process().memory_info().rss)))
""")
                assert reply['status'] == 'ok', reply
                evidence = json.loads(output.strip())
                assert Path(evidence['prefix']) == environment
                assert Path(evidence['base']) == self.python.parent.parent
                assert evidence['humanize'] == ('4.13.0' if label == 'a' else '4.14.0')
                for name in ['native', 'kernel', 'arrow']:
                    assert Path(evidence[name]).is_relative_to(environment)
                records = self.processes()
                kernel = next(p for p in records if p['role'] == 'notebook-kernel-' + entry['session_id'])
                server = next(p for p in records if p['role'].startswith('notebook-server-'))
                assert str(environment / 'bin/python') in psutil.Process(kernel['pid']).cmdline()
                assert Path(psutil.Process(server['pid']).cmdline()[0]).resolve() == self.python.resolve()
                self.check('project_' + label + '_real_gate_bounded_session_native_import_and_spark',
                           humanize=evidence['humanize'], kernel_rss_bytes=evidence['rss'],
                           server_rss_bytes=psutil.Process(server['pid']).memory_info().rss,
                           prefix_is_project=True, base_prefix_is_service=True,
                           kernel_arrow_native_imports_are_project_local=True)
                busy = asyncio.Event()
                execution = asyncio.create_task(self.execute(ws, "import time\nprint('NE01_BUSY',flush=True)\ntime.sleep(60)", busy))
                await asyncio.wait_for(busy.wait(), 10)
                self.action(action='interrupt', id=entry['id'], generation=entry['generation'], key='interrupt')
                reply, _ = await execution
                assert reply['status'] == 'error' and reply['ename'] == 'KeyboardInterrupt'
                old_pid = kernel['pid']
                entry = self.action(action='restart', id=entry['id'], generation=entry['generation'], key='restart')
                entry = await self.state(entry, ['ready'])
                assert entry['epoch_id'] == epoch
                assert not psutil.pid_exists(old_pid)
                ws.close()
                ws = await self.connect(entry)
                reply, output = await self.execute(ws,
                    f"import sys\nassert sys.prefix == {str(environment)!r}\nassert spark.table('public.orders').count()==2\nprint('RESTART_OK')")
                assert reply['status'] == 'ok' and 'RESTART_OK' in output
                self.check('project_' + label + '_interrupt_restart_preserves_environment_and_epoch')
            finally:
                ws.close()
                self.action(action='shutdown', id=entry['id'], generation=entry['generation'], key='stop')
                await self.state(entry, ['stopped'])
            assert not any(p['role'].startswith('notebook-kernel-') for p in self.processes())
            self.cli(project, 'down')
        assert inventory(self.service / 'python') == self.service_before
        self.run([self.service / 'bin/supabricks', 'installation', 'verify'])
        self.check('service_bytes_unchanged_and_all_kernels_reaped')

    async def main(self):
        try:
            self.environment_tests()
            await self.product_tests()
            self.report['status'] = 'passed'
        except BaseException as error:
            self.report['status'] = 'failed'
            self.report['failure_type'] = type(error).__name__
            raise
        finally:
            for project in self.projects:
                try:
                    self.cli(project, 'down')
                except Exception:
                    self.report['cleanup_failed'] = True
            self.save()
            print(json.dumps({'status': self.report['status'], 'private_workspace': str(self.root)}))
            if self.report['status'] == 'passed' and not self.report.get('cleanup_failed'):
                shutil.rmtree(self.root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--target', required=True)
    asyncio.run(Probe(parser.parse_args()).main())
