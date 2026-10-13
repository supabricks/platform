"""A real server on a temporary data directory. Nothing is mocked.

This is the local profile without engines: the daemon, its state and the
console HTTP server. It is enough for every operation that does not need a
running PostgreSQL, and it starts in about a second.
"""
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import requests

REPO = pathlib.Path(__file__).resolve().parents[3]
# A Python with the import worker's packages, or None. Set by conftest.py.
WORKER = None


class Server:
    def __init__(self, binary):
        self._tmp = tempfile.TemporaryDirectory(prefix='sb-api-', dir='/tmp')
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.root.chmod(0o700)
        # A copy, so a rebuild during the run cannot replace a running daemon.
        self.binary = self.root / 'supabricks'
        shutil.copy2(binary, self.binary)
        self.data = self.root / 'data'
        self.project = self.root / 'project'
        self.assets = self.root / 'assets'
        self.definition = str(uuid.uuid4())
        self.daemon = None
        self.base = None

    def start(self):
        self.project.mkdir()
        (self.project / 'supabricks.toml').write_text(
            f'format_version=1\nid="{self.definition}"\nname="api-tests"\n')
        self.assets.mkdir()
        html = b'<html><head></head><body>API tests</body></html>'
        (self.assets / 'index.html').write_bytes(html)
        (self.assets / 'console.json').write_text(json.dumps(
            dict(api_version=1, files={'index.html': hashlib.sha256(html).hexdigest()})))
        self._spawn()
        if WORKER:
            # What `supabricks analytics configure` writes: where the workers
            # that read and load files are. Imports need it, and nothing else
            # here uses it.
            configured = self.data / 'analytics.json'
            configured.write_text(json.dumps(dict(
                python=str(WORKER), worker=str(REPO / 'python/analytics/export.py'))))
            configured.chmod(0o600)
        context = self.rpc(method='resolve_binding',
                           source=dict(definition_id=self.definition, worktree=str(self.project)))
        self.binding = dict(project_id=context['runtime_project_id'], worktree=str(self.project))
        self.open_console()
        return self

    def _spawn(self):
        self._log = (self.root / 'daemon.log').open('ab')
        self.daemon = subprocess.Popen([str(self.binary), 'daemon', '--data-dir', str(self.data)],
                                       stdout=self._log, stderr=self._log)
        wait(lambda: (self.data / 'control.sock').exists() and self._alive())

    def _alive(self):
        try:
            self.rpc(method='status')
            return True
        except (OSError, AssertionError):
            return False

    def rpc(self, **request):
        """One request on the daemon's control socket, as the command line sends it."""
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(10)
            s.connect(str(self.data / 'control.sock'))
            s.sendall(json.dumps(dict(version=1, request=request)).encode() + b'\n')
            value = json.loads(s.makefile('rb').readline())
        assert 'error' not in value, value
        return value['result']

    def try_rpc(self, **request):
        """Like rpc, but returns (result, error) instead of failing."""
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(10)
            s.connect(str(self.data / 'control.sock'))
            s.sendall(json.dumps(dict(version=1, request=request)).encode() + b'\n')
            value = json.loads(s.makefile('rb').readline())
        return value.get('result'), value.get('error')

    def new_source(self, name):
        """A project source on disk, as `supabricks project init` would leave it."""
        source = str(uuid.uuid4())
        folder = self.root / 'sources' / str(uuid.uuid4())
        folder.mkdir(parents=True)
        (folder / 'supabricks.toml').write_text(
            f'format_version=2\nid="{source}"\nname="{name}"\n'
            '[package]\nversion="0.1.0"\ninclude=[]\nnotebook_outputs="strip"\n')
        return folder

    @staticmethod
    def definition_of(folder):
        """The source ID written in a project folder's manifest."""
        text = (pathlib.Path(folder) / 'supabricks.toml').read_text()
        return next(line.split('"')[1] for line in text.splitlines() if line.startswith('id='))

    def copy_source(self, folder):
        """A second checkout of the same source."""
        copy = self.root / 'sources' / str(uuid.uuid4())
        shutil.copytree(folder, copy)
        return copy

    def create_project(self, folder, name=None):
        """Create a project from a source folder, as the command line does.

        Returns (context, error); exactly one is set.
        """
        definition = next(line.split('"')[1] for line in (folder / 'supabricks.toml').read_text().splitlines()
                          if line.startswith('id='))
        command = dict(action='create', key=str(uuid.uuid4()))
        if name is not None:
            command['name'] = name
        return self.try_rpc(method='project', source=dict(definition_id=definition, worktree=str(folder)),
                            command=command)

    def open_console(self):
        """Launch the HTTP server. Returns a one-time launch token for a session."""
        def launch():
            result = self.rpc(method='console_open', binding=self.binding, assets=str(self.assets))
            return result if result['state'] == 'ready' else None
        self.base, token = wait(launch)['url'].split('/#launch=')
        return token

    def session(self):
        """A signed-in client, as a browser would be after launching the console."""
        from .client import Client
        http = requests.Session()
        http.trust_env = False
        response = http.post(self.base + '/api/session', json=dict(token=self.open_console()),
                             headers={'x-supabricks-console': '1', 'Origin': self.base}, timeout=10)
        assert response.status_code == 200, response.text
        return Client(self.base, http, response.json()['csrf'])

    def anonymous(self):
        from .client import Client
        http = requests.Session()
        http.trust_env = False
        return Client(self.base, http, None)

    def restart(self, kill=False):
        """Stop the daemon and start it again on the same data directory."""
        if kill:
            self.daemon.kill()
        else:
            self.rpc(method='shutdown')
        self.daemon.wait(timeout=15)
        self._spawn()
        self.open_console()

    def stop(self):
        if self.daemon and self.daemon.poll() is None:
            try:
                self.rpc(method='shutdown')
                self.daemon.wait(timeout=15)
            except Exception:
                self.daemon.kill()
                self.daemon.wait(timeout=15)
        self._log.close()

    def log(self):
        return (self.root / 'daemon.log').read_text(errors='replace')

    def cleanup(self):
        self._tmp.cleanup()


class EngineServer(Server):
    """The same server with database engines: PostgreSQL, storage and the rest.

    It needs a verified engine bundle and helpers (see e2e/native/README.md),
    takes tens of seconds to start, and is used only by tests that must see
    work finish. It cannot be killed and restarted by a test.
    """

    def __init__(self, binary, bundle, helpers):
        super().__init__(binary)
        self.bundle, self.helpers = bundle, helpers

    def _cli(self, *args, timeout=300):
        # Make a backup schedule's hour three seconds long.
        return subprocess.run([str(self.binary), *args, '--data-dir', str(self.data)],
                              capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, SUPABRICKS_TEST_BACKUP_HOUR_MS='3000'))

    def _spawn(self):
        self._log = (self.root / 'daemon.log').open('ab')
        result = self._cli('up', '--bundle', str(self.bundle), '--helpers', str(self.helpers))
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        wait(lambda: self._alive() and self.rpc(method='status').get('runtime', {}).get('ready'), timeout=120)

    def restart(self, kill=False):
        raise NotImplementedError('the engine-backed server is not restarted by tests')

    def stop(self):
        self._cli('down')
        self._log.close()

    def log(self):
        return self._cli('doctor', timeout=30).stdout


class GovernedServer(Server):
    """The same server with the API also served to signed-in callers.

    Callers are service accounts, which the owner of the machine creates and
    gives roles through the control socket, so no identity provider is needed.
    The API is served by `supabricks identity api`, which checks the token and
    the caller's role in the project exactly as the governed console does.
    """

    def start(self):
        super().start()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.governed = f'http://127.0.0.1:{port}'
        self._api = subprocess.Popen(
            [str(self.binary), 'identity', 'api', '--listen', f'127.0.0.1:{port}', '--data-dir', str(self.data)],
            stdout=self._log, stderr=self._log)
        wait(lambda: self._listening(port))
        return self

    @staticmethod
    def _listening(port):
        with socket.socket() as probe:
            return probe.connect_ex(('127.0.0.1', port)) == 0

    def principal(self, label):
        """A new service account: (its ID, a client signed in as it)."""
        principal = self.rpc(method='identity_admin', command=dict(action='service', label=label))['principal_id']
        return principal, self.sign_in(principal)

    def sign_in(self, principal):
        from .client import Client
        issued = self.rpc(method='identity_admin', command=dict(
            action='issue_service', principal=principal, scopes=['identity:self', 'project:control'],
            ttl_seconds=3600))
        return self.bearer(issued['token'])

    def bearer(self, token):
        from .client import Client
        http = requests.Session()
        http.trust_env = False
        return Client(self.governed, http, None, {'Authorization': f'Bearer {token}'} if token else {})

    def grant(self, project, subject, role, kind='principal'):
        """Give a principal or group a role in a project, or remove it with None."""
        deployment = str(uuid.UUID(project.removeprefix('prj_')))
        revision = self.rpc(method='authorization_admin',
                            command=dict(action='policy', deployment=deployment))['policy_revision']
        self.rpc(method='authorization_admin', command=dict(
            action='set_role', deployment=deployment, subject=dict(kind=kind, id=subject), role=role,
            expected_policy=revision, key=str(uuid.uuid4())))

    def audit(self, project):
        deployment = str(uuid.UUID(project.removeprefix('prj_')))
        return self.rpc(method='authorization_admin',
                        command=dict(action='audit', deployment=deployment, after=0))['events']

    def stop(self):
        if getattr(self, '_api', None) and self._api.poll() is None:
            self._api.terminate()
            self._api.wait(timeout=15)
        super().stop()


def wait(call, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = call()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError('timed out waiting for the server')
