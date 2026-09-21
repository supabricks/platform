"""Disposable IAM00 harness utilities. Never log bearer tokens or raw bodies."""
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / 'pins.lock.json').read_text())


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def command(*args, timeout=120, **kwargs):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed with exit {result.returncode}; private diagnostics required')
    return result.stdout.strip()


def wait(action, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = action()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise TimeoutError('bounded readiness deadline exceeded')


def request(url, method='GET', body=None, token=None, form=False, opener=None):
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        body = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(1024 * 1024)
        try:
            result = json.loads(raw)
        except ValueError:
            result = raw.decode(errors='replace')
        return response.status, result, dict(response.headers)


class Evidence:
    def __init__(self):
        self.checks = []
        self.limitations = []
        self.metrics = {}

    def check(self, name, condition, **facts):
        if not condition:
            raise AssertionError(name)
        self.checks.append(dict(name=name, status='PASS', **facts))
        print('PASS ' + name, flush=True)

    def limit(self, name, **facts):
        self.limitations.append(dict(name=name, **facts))
        print('NO-GO ' + name, flush=True)
