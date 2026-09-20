"""Private UC00 server. Synthetic JWTs are test fixtures, not an identity provider."""
import base64
import json
import ipaddress
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import psutil
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith(
        ('AWS_', 'AZURE_', 'GOOGLE_', 'DATABRICKS_', 'UNITY_', 'UC_', 'SAIL_'))
        and 'proxy' not in k.lower()}


class Server:
    def __init__(self, runtime, root, s3):
        self.runtime, self.root = runtime, root
        self.secrets = [s3["s3_access"], s3["s3_secret"]]
        root.mkdir(mode=0o700)
        shutil.copytree(runtime/'configuration-template', root/'etc/conf')
        (root/'etc/db').mkdir()
        config = root/'etc/conf/server.properties'
        config.write_text('''server.env=dev
server.authorization=enable
server.allowed-issuers=http://127.0.0.1/uc00
server.audiences=uc00
server.access-token-timeout=PT5M
s3.bucketPath.0=s3://supabricks
s3.region.0=us-east-1
''' + f"s3.accessKey.0={s3['s3_access']}\ns3.secretKey.0={s3['s3_secret']}\ns3.sessionToken.0=uc00-test-only-no-downscoping\n")
        config.chmod(0o600)
        # Upstream main starts the transcoder on N and the API on N+1.
        # Check both ports, not just the public one.
        for _ in range(100):
            with socket.socket() as front, socket.socket() as backend:
                front.bind(('127.0.0.1', 0))
                self.port = front.getsockname()[1]
                if self.port == 65535: continue
                try: backend.bind(('127.0.0.1', self.port+1))
                except OSError: continue
                break
        else: raise RuntimeError('no adjacent UC probe ports available')
        self.base = f'http://127.0.0.1:{self.port}'
        self.process = None
        self.peak = 0
        self.ready_times = []
        self.idle_samples = []

    def request(self, method, path, body=None, token=None, control=False):
        prefix = '/api/1.0/unity-control/' if control else '/api/2.1/unity-catalog/'
        if token is None:
            token = (self.root/'etc/conf/token.txt').read_text().strip()
        headers = {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}
        req = urllib.request.Request(self.base+prefix+path,
            data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read()
                try: data = json.loads(raw) if raw else None
                except ValueError: data = raw.decode(errors='replace')
                return response.status, data
        except urllib.error.HTTPError as error:
            raw = error.read()
            try: data = json.loads(raw)
            except ValueError: data = {'error': raw.decode(errors='replace')[:500]}
            return error.code, data

    def ok(self, method, path, body=None, **kwargs):
        status, data = self.request(method, path, body, **kwargs)
        assert 200 <= status < 300, (method, path, status, data)
        return data

    def start(self):
        classpath = os.pathsep.join(str(self.runtime/p) for p in json.loads((self.runtime/'classpath.json').read_text()))
        began = time.monotonic()
        with (self.root/'server.log').open('ab') as log:
            self.process = subprocess.Popen([str(self.runtime/'java/bin/java'), '-Xms64m', '-Xmx256m',
                '-XX:ActiveProcessorCount=2', '-Djava.net.preferIPv4Stack=true', '-cp', classpath,
                'io.unitycatalog.server.UnityCatalogServer', '--port', str(self.port)],
                cwd=self.root, env=clean_env(), stdout=log, stderr=subprocess.STDOUT)
        proc = self.process
        def sample():
            while proc.poll() is None:
                try: self.peak = max(self.peak, psutil.Process(proc.pid).memory_info().rss)
                except psutil.NoSuchProcess: return
                time.sleep(.1)
        self.sampler = threading.Thread(target=sample, daemon=True)
        self.sampler.start()
        while time.monotonic()-began < 45:
            assert proc.poll() is None, 'UC exited; inspect private server.log'
            try:
                if self.request('GET', 'catalogs')[0] == 200: break
            except (OSError, ValueError): pass
            time.sleep(.1)
        else: raise AssertionError('UC readiness deadline exceeded')
        self.ready_times.append(round(time.monotonic()-began, 3))
        listeners = psutil.Process(proc.pid).net_connections(kind='inet')
        for connection in listeners:
            if connection.status == 'LISTEN':
                address = ipaddress.ip_address(connection.laddr.ip)
                mapped = getattr(address, 'ipv4_mapped', None)
                assert (mapped or address).is_loopback, connection.laddr
        for _ in range(10):
            self.idle_samples.append(psutil.Process(proc.pid).memory_info().rss)
            time.sleep(.1)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=5)
                raise AssertionError('UC needed forced shutdown')
            finally: self.sampler.join(timeout=2)

    def token(self, email, expires=300):
        """Mint solely inside the private probe using UC's own generated test key."""
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
        conf = self.root/'etc/conf'
        header = encode(dict(alg='RS512', typ='JWT', kid=(conf/'key_id.txt').read_text().strip()))
        now = int(time.time())
        body = encode(dict(iss='internal', sub=email, iat=now, exp=now+expires, type='ACCESS'))
        payload = header+b'.'+body
        key = serialization.load_der_private_key((conf/'private_key.der').read_bytes(), password=None)
        signature = key.sign(payload, padding.PKCS1v15(), hashes.SHA512())
        return (payload+b'.'+base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()

    def user(self, email):
        return self.ok('POST', 'scim2/Users', dict(userName=email, displayName=email,
            active=True, emails=[dict(value=email, primary=True)]), control=True)

    def grant(self, kind, name, email, add=(), remove=()):
        return self.ok('PATCH', f'permissions/{kind}/{name}', dict(changes=[
            dict(principal=email, add=list(add), remove=list(remove))]))

    def diagnostics(self):
        """Bounded, redacted Java failure details; never upload the private tree."""
        secrets = list(self.secrets)
        token_file = self.root/'etc/conf/token.txt'
        if token_file.exists(): secrets.append(token_file.read_text().strip())
        lines = []
        for path in (self.root/'server.log', self.root/'etc/logs/server.log'):
            if path.exists():
                for line in path.read_text(errors='replace').splitlines():
                    if not any(word in line for word in ('Exception', 'Error', 'ERROR', 'Caused by:', '\tat ')):
                        continue
                    for secret in secrets:
                        if secret: line = line.replace(secret, '[redacted]')
                    line = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted JWT]', line)
                    lines.append(line[:500])
        return dict(exit_code=None if self.process is None else self.process.poll(), lines=lines[-35:])
