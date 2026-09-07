#!/usr/bin/env python3
"""Exercise the actual curl installer and installed app/branch workflow.

Python and psycopg are test/application inputs, not database runtime dependencies.
Run this inside the clean Linux image with --network=none to qualify offline use.
"""
import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from stage import stage


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def run(argv, env=None, cwd=None, timeout=180):
    p = subprocess.run(list(map(str, argv)), env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        # Never include arguments or stdout here: connect returns credentials.
        raise AssertionError(f'{Path(str(argv[0])).name}: exit {p.returncode}: {p.stderr[-3000:]}')
    return p.stdout


def qualify(args):
    if args.minimal_host:
        available = [tool for tool in ['cargo', 'rustc', 'go', 'gcc', 'cc', 'clang', 'docker', 'java'] if shutil.which(tool)]
        assert not available, f'clean qualification contains build/cloud tooling: {available}'
        assert '16.' in run(['/usr/bin/psql', '--version']), 'clean fixture must include an existing system PostgreSQL client'
    workspace = Path(tempfile.mkdtemp(prefix='sb-r01-', dir='/tmp'))
    prefix = workspace / "programs ' with spaces"
    data = workspace / 'data'
    project = workspace / 'app'
    env = dict(os.environ, SUPABRICKS_INSTALL_DIR=str(prefix), SUPABRICKS_NO_MODIFY_PATH='1',
               SUPABRICKS_DATA_DIR=str(data))
    for key in list(env):
        if key.startswith(('PG', 'AWS_', 'PC_', 'OTEL_')) or key.upper().endswith('_PROXY'):
            env.pop(key)
    key = workspace / 'preview.pem'
    run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', key])
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(args.directory.resolve())))
    base = f'http://127.0.0.1:{server.server_port}'
    stage(args.directory.resolve(), args.version, base, key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    checks = []
    binary = prefix / 'bin/supabricks'
    app = None
    app_log = None

    def install():
        # Actual curl stdout is Bash stdin; no downloaded script bypass.
        curl = subprocess.Popen(['curl', '-fsSL', base + '/install.sh'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        bash = subprocess.run(['bash'], stdin=curl.stdout, capture_output=True, text=True, env=env, timeout=240)
        curl.stdout.close()
        curl.wait(timeout=10)
        assert curl.returncode == 0 and bash.returncode == 0, bash.stderr

    def cli(*argv):
        return json.loads(run([binary, *argv, '--project', project], env=env, cwd='/tmp'))

    def sql(query, branch='main', write=False):
        return cli('sql', '--sql', query, '--branch', branch, *(['--write'] if write else []))

    try:
        started = time.monotonic()
        install()
        install_seconds = time.monotonic() - started
        identity = json.loads(run([binary, 'installation', 'verify'], env=env))['identity']
        install()
        assert json.loads(run([binary, 'installation', 'verify'], env=env))['identity'] == identity
        assert '17.8' in run([prefix / 'bin/psql', '--version'], env=env)
        checks.append('signed curl-to-bash install and repeat install; custom path with spaces and quote; bundled psql')
        # Occupy the conventional PG port if another Postgres is not there already.
        reserved = socket.socket()
        try:
            reserved.bind(('127.0.0.1', 5432))
            reserved.listen()
        except OSError:
            reserved.close()
        shutil.copytree(prefix / 'current/examples/orders', project)
        cli('init', 'orders')
        cli('up')
        assert cli('doctor')['healthy']
        cli('database', 'create', 'main', '--wait')
        cli('branch', 'use', 'main')
        cli('sql', '--file', project / 'migrations/001-orders.sql', '--branch', 'main', '--write')
        connection = cli('connect', 'main')['uri']
        assert ':5432/' not in connection
        app_log = (workspace / 'app.log').open('w')
        app = subprocess.Popen([sys.executable, str(project / 'app.py'), '--port', '0'],
                               env=dict(env, DATABASE_URL=connection), stdout=subprocess.PIPE,
                               stderr=app_log, text=True)
        assert select.select([app.stdout], [], [], 15)[0], 'sample app readiness timeout'
        line = app.stdout.readline()
        assert ' on ' in line, 'sample app did not start; inspect app.log'
        url = line.strip().split(' on ', 1)[1]
        req = urllib.request.Request(url, json.dumps(dict(customer='Ada', total_cents=1299)).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            assert response.status == 201
        with urllib.request.urlopen(url, timeout=30) as response:
            assert json.load(response)['orders'][0]['customer'] == 'Ada'
        app.terminate(); app.wait(timeout=10); app = None
        checks.append('automatic bundled startup, occupied port 5432, real orders HTTP app writes and reads')
        cli('branch', 'create', 'experiment', '--from', 'main', '--wait')
        cli('sql', '--file', project / 'migrations/002-status.sql', '--branch', 'experiment', '--write')
        sql("UPDATE orders SET status='paid'", branch='experiment', write=True)
        assert sql('SELECT status FROM orders', branch='experiment')['rows'] == [['paid']]
        assert sql("SELECT count(*) FROM information_schema.columns WHERE table_name='orders' AND column_name='status'")['rows'] == [['0']]
        checks.append('branch migration and data isolation through public commands')
        # Exercise the packaged stdio server. P06 separately covers tool calls.
        mcp = subprocess.Popen([str(binary), 'mcp', '--project', str(project)], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            messages = [
                dict(jsonrpc='2.0', id=1, method='initialize', params=dict(protocolVersion='2025-06-18', capabilities={}, clientInfo=dict(name='installed-alpha', version='1'))),
                dict(jsonrpc='2.0', method='notifications/initialized'),
                dict(jsonrpc='2.0', id=2, method='tools/list', params={}),
            ]
            for message in messages:
                mcp.stdin.write(json.dumps(message) + '\n')
                mcp.stdin.flush()
                if 'id' in message:
                    assert select.select([mcp.stdout], [], [], 15)[0]
                    response = json.loads(mcp.stdout.readline())
                    assert 'error' not in response, response
            assert len(response['result']['tools']) == 25
            checks.append('installed MCP initialization and complete tool discovery')
        finally:
            mcp.stdin.close()
            mcp.wait(timeout=10)
        cli('branch', 'suspend', 'main', '--wait')
        assert sql('SELECT customer FROM orders')['rows'] == [['Ada']]
        cli('down')
        # A different release may not silently open this data root. Reuse the
        # actual installed executable with changed metadata to exercise the
        # compatibility guard, then restore the exact original release.
        manifest_path = (prefix / 'current/release.json').resolve()
        original_manifest = manifest_path.read_bytes()
        changed = json.loads(original_manifest)
        changed['version'] = 'v0.0.0-rejected'
        try:
            manifest_path.write_text(json.dumps(changed))
            rejected = subprocess.run([str(binary), 'up'], env=env, capture_output=True, text=True, timeout=90)
            assert rejected.returncode != 0, 'different release opened existing data'
        finally:
            manifest_path.write_bytes(original_manifest)
        moved = workspace / 'moved programs'
        prefix.rename(moved)
        prefix = moved
        binary = prefix / 'bin/supabricks'
        env['SUPABRICKS_INSTALL_DIR'] = str(prefix)
        cli('up')
        assert cli('connect', 'main')['uri'] == connection
        assert sql('SELECT customer FROM orders')['rows'] == [['Ada']]
        assert sql('SELECT status FROM orders', branch='experiment')['rows'] == [['paid']]
        assert '17.8' in run([prefix / 'bin/psql', '--version'], env=env)
        checks.append('wake on connect, incompatible release rejection, stopped installation relocation, stable URI and retained parent/branch data')
        install()
        assert cli('connect', 'main')['uri'] == connection
        cli('branch', 'delete', 'experiment', '--wait')
        cli('down')
        checks.append('repeat install while running, branch cleanup and retained data on shutdown')
        reserved.close()
        report = dict(status='passed', host=platform.platform(), workspace=str(workspace),
                      release_identity=identity, install_seconds=round(install_seconds, 2),
                      checks=checks, network_qualification=args.network_evidence)
    except BaseException as error:
        report = dict(status='failed', host=platform.platform(), workspace=str(workspace), checks=checks,
                      error=str(error), network_qualification=args.network_evidence)
        raise
    finally:
        if app is not None:
            app.terminate(); app.wait(timeout=10)
        if app_log is not None:
            app_log.close()
        if binary.exists():
            subprocess.run([str(binary), 'down'], env=env, capture_output=True, timeout=90)
        server.shutdown(); server.server_close()
        key.unlink(missing_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        # Reports/logs are retained; successful database state is disposable.
        if report['status'] == 'passed' and not args.keep:
            shutil.rmtree(workspace)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--version', default='v0.1.0-alpha.1')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--minimal-host', action='store_true', help='assert no build tools and an existing system psql 16')
    parser.add_argument('--network-evidence', default='not externally isolated; see separate observation report')
    qualify(parser.parse_args())
