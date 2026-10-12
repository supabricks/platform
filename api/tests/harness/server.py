"""A real server on a temporary data directory. Nothing is mocked.

This is the local profile without engines: the daemon, its state and the
console HTTP server. It is enough for every operation that does not need a
running PostgreSQL, and it starts in about a second.
"""
import hashlib
import json
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import requests


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


def wait(call, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = call()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError('timed out waiting for the server')
