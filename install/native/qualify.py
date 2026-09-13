#!/usr/bin/env python3
"""Exercise the actual curl installer and installed app/branch workflow.

Python and psycopg are test/application inputs, not database runtime dependencies.
Run this inside the clean Linux image with --network=none to qualify offline use.
"""
import argparse
from contextlib import contextmanager
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
        # Connect can return credentials. Preserve only explicit error/status
        # fields so failed exports remain diagnosable after container teardown.
        diagnostic = {}
        try:
            value = json.loads(p.stdout)
            diagnostic = {key: value[key] for key in ('state', 'error') if key in value}
            if isinstance(value.get('export'), dict):
                diagnostic['export_outcome'] = value['export'].get('outcome')
        except (ValueError, TypeError, AttributeError):
            pass
        raise AssertionError(f'{Path(str(argv[0])).name}: exit {p.returncode}: {p.stderr[-3000:]} {json.dumps(diagnostic)}')
    return p.stdout


def benchmark(binary, prefix, env, project, workspace, measurements, measured):
    available = shutil.disk_usage(workspace).free
    if available < 20 * 1024**3:
        raise AssertionError(f'1 GB qualification needs 20 GiB free scratch space before loading; available={available} bytes')
    def cli(*args):
        return json.loads(run([binary, *args, '--project', project], env=env, timeout=1900))

    def disk():
        paths = [p for p in Path(env['SUPABRICKS_DATA_DIR']).rglob('*') if p.is_file()]
        sizes = []
        for path in paths:
            try:
                sizes.append(path.stat())
            except FileNotFoundError:
                pass
        return dict(logical_bytes=sum(s.st_size for s in sizes), allocated_bytes=sum(s.st_blocks * 512 for s in sizes))

    cli('database', 'create', 'scale', '--wait')
    connection = cli('connect', 'scale')['uri']
    script = workspace / 'load-scale.py'
    script.write_text("""import os
import psycopg
with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as connection:
    connection.execute('DROP TABLE IF EXISTS payload')
    connection.execute('CREATE TABLE payload(id bigint, value text)')
    # 1024 bytes per row from 32 distinct MD5 digests. Deterministic data with
    # moderate hexadecimal compressibility, not random binary or real app data.
    expression = ' || '.join("md5(g::text || ':" + str(j) + "')" for j in range(32))
    rows = int(os.environ['BENCH_ROWS'])
    for first in range(1, rows + 1, 10000):
        connection.execute('INSERT INTO payload SELECT g, ' + expression + ' FROM generate_series(%s::bigint, %s::bigint) g', (first, min(rows, first + 9999)))
""")
    for size in (10_000_000, 100_000_000, 1_000_000_000):
        rows = size // 1024
        label = f'snapshot_{size}_bytes'
        before = disk()
        started = time.monotonic()
        run([prefix / 'current/python/analytics/python', script],
            env=dict(env, DATABASE_URL=connection, BENCH_ROWS=str(rows)), timeout=1200)
        load_seconds = time.monotonic() - started
        with measured(label):
            refresh = cli('analytics', 'refresh', '--branch', 'scale', '--wait',
                          '--max-bytes', str(2 * 1024**3), '--timeout-ms', '1800000')
        with measured(label + '_query'):
            query = cli('analytics', 'sql', '--branch', 'scale', '--sql',
                        'SELECT count(*), sum(length(value)) FROM public.payload', '--timeout-ms', '30000')
            assert query['rows'] == [[str(rows), str(rows * 1024)]], query
        measurements[label].update(source_payload_bytes=rows * 1024, rows=rows,
            load_seconds=load_seconds, disk_before=before, disk_after=disk(),
            export_payload_mb_per_second=(rows * 1024 / 1_000_000) / measurements[label]['elapsed_seconds'])
        cli('analytics', 'gc', '--branch', 'scale', '--keep', '1')
    cli('branch', 'delete', 'scale', '--wait')


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
    measurements = {}
    samplers = []

    @contextmanager
    def measured(name):
        stop = workspace / (name + '.stop')
        report = workspace / (name + '.json')
        sampler = subprocess.Popen([str(prefix / 'current/python/analytics/python'),
            str(Path(__file__).with_name('measure.py').resolve()), '--data', str(data),
            '--stop', str(stop), '--report', str(report)], env=env)
        samplers.append((sampler, stop))
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            stop.touch()
            assert sampler.wait(timeout=15) == 0, 'resource sampler failed'
            measurements[name] = dict(elapsed_seconds=elapsed, **json.loads(report.read_text()))

    def analytics(query, session=None, branch='main'):
        target = ['--session', session] if session else ['--branch', branch]
        return cli('analytics', 'sql', '--sql', query, '--timeout-ms', '30000', *target)

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
        measurements['distribution'] = dict(archive_bytes=sum(p.stat().st_size for p in args.directory.glob('*.tar.gz')),
            unpacked_bytes=sum(p.stat().st_size for p in (prefix / 'current').resolve().rglob('*') if p.is_file()))
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
        with measured('cold_runtime_start'):
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
        poison = workspace / 'poison-python'
        poison.mkdir()
        (poison / 'sitecustomize.py').write_text("raise RuntimeError('host Python environment leaked into the private runtime')\n")
        env['PYTHONPATH'] = str(poison)
        env['PYTHONHOME'] = str(poison)
        assert not (data / 'analytics.json').exists(), 'installer must discover the private worker automatically'
        with measured('first_analytical_query'):
            first = cli('analytics', 'open', '--branch', 'main', '--ttl-ms', '600000', '--wait')
            assert first['state'] == 'ready', first
            assert analytics('SELECT sum(total_cents) FROM public.orders', session=first['id'])['rows'] == [['1299']]
        with measured('warm_analytical_query'):
            assert analytics('SELECT sum(total_cents) FROM public.orders', session=first['id'])['rows'] == [['1299']]
        sql('UPDATE orders SET total_cents=2599', write=True)
        with measured('analytical_refresh'):
            cli('analytics', 'refresh', '--branch', 'main', '--wait')
        assert analytics('SELECT sum(total_cents) FROM public.orders', session=first['id'])['rows'] == [['1299']]
        assert analytics('SELECT sum(total_cents) FROM public.orders')['rows'] == [['2599']]
        cli('analytics', 'close', first['id'], '--wait')
        script = workspace / 'dataframe.py'
        script.write_text("assert spark.table('public.orders').first().total_cents == 2599\nassert epoch['epoch_id']\n")
        run([binary, 'spark', 'shell', '--branch', 'main', '--file', script, '--project', project], env=env)
        assert json.loads(run([binary, 'installation', 'verify'], env=env))['identity'] == identity
        checks.append('automatic private analytics, first snapshot, pinned refresh, SQL, ordinary DataFrame shell, immutable installed files')
        cli('branch', 'create', 'experiment', '--from', 'main', '--wait')
        cli('sql', '--file', project / 'migrations/002-status.sql', '--branch', 'experiment', '--write')
        sql("UPDATE orders SET status='paid'", branch='experiment', write=True)
        assert sql('SELECT status FROM orders', branch='experiment')['rows'] == [['paid']]
        assert sql("SELECT count(*) FROM information_schema.columns WHERE table_name='orders' AND column_name='status'")['rows'] == [['0']]
        assert analytics('SELECT status FROM public.orders', branch='experiment')['rows'] == [['paid']]
        checks.append('branch migration, data isolation and analytical snapshot of the migrated branch')
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
            tools = response['result']['tools']
            names = {tool['name'] for tool in tools}
            assert len(names) == len(tools), 'duplicate MCP tool names'
            # P06 compares the complete shared schema fixture. This isolated
            # installer harness checks the capabilities its user workflows need.
            assert {'capabilities', 'catalog', 'sql', 'create_branch', 'select_branch',
                    'env_inspect', 'env_initialize', 'env_manage', 'env_status',
                    'env_find', 'env_declaration', 'env_cancel', 'env_collect'} <= names
            def call_tool(name, arguments):
                mcp.stdin.write(json.dumps(dict(jsonrpc='2.0', id=3, method='tools/call',
                    params=dict(name=name, arguments=arguments))) + '\n')
                mcp.stdin.flush()
                assert select.select([mcp.stdout], [], [], 30)[0], 'MCP tool timeout'
                result = json.loads(mcp.stdout.readline())['result']
                assert not result.get('isError'), result
                return result['structuredContent']
            opened = call_tool('analytics_open', dict(branch='main', key='installed-preview-session'))
            session_id = opened['id']
            for _ in range(120):
                session = call_tool('analytics_session', dict(id=session_id))
                if session['state'] != 'starting' and session['state'] != 'waiting':
                    break
                time.sleep(0.25)
            assert session['state'] == 'ready', session
            submitted = call_tool('analytics_sql', dict(id=session_id, sql='SELECT sum(total_cents) FROM public.orders'))
            for _ in range(120):
                result = call_tool('analytics_query', dict(id=session_id, query=submitted['id']))
                if result['state'] != 'running' and result['state'] != 'queued':
                    break
                time.sleep(0.25)
            assert result['rows'] == [['2599']], result
            call_tool('analytics_close', dict(id=session_id))
            checks.append('installed MCP discovery and actual analytical session/query/close tool calls')
        finally:
            mcp.stdin.close()
            mcp.wait(timeout=10)
        cli('branch', 'suspend', 'main', '--wait')
        with measured('connection_wake'):
            assert sql('SELECT customer FROM orders')['rows'] == [['Ada']]
        with measured('idle_runtime'):
            time.sleep(5)
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
        assert analytics('SELECT sum(total_cents) FROM public.orders')['rows'] == [['2599']]
        cli('analytics', 'refresh', '--branch', 'main', '--wait')
        assert analytics('SELECT sum(total_cents) FROM public.orders')['rows'] == [['2599']]
        assert '17.8' in run([prefix / 'bin/psql', '--version'], env=env)
        checks.append('wake on connect, incompatible release rejection, stopped installation relocation, stable URI and retained parent/branch data')
        install()
        assert cli('connect', 'main')['uri'] == connection
        if args.benchmarks:
            benchmark(binary, prefix, env, project, workspace, measurements, measured)
        cli('branch', 'delete', 'experiment', '--wait')
        cli('down')
        checks.append('repeat install while running, branch cleanup and retained data on shutdown')
        reserved.close()
        report = dict(status='passed', host=platform.platform(), workspace=str(workspace),
                      release_identity=identity, install_seconds=round(install_seconds, 2),
                      checks=checks, measurements=measurements, network_qualification=args.network_evidence)
    except BaseException as error:
        report = dict(status='failed', host=platform.platform(), workspace=str(workspace), checks=checks,
                      error=str(error), measurements=measurements, network_qualification=args.network_evidence)
        raise
    finally:
        for sampler, stop in samplers:
            if sampler.poll() is None:
                stop.touch()
                sampler.wait(timeout=15)
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
    parser.add_argument('--version', default='v0.1.0-alpha.15')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--benchmarks', action='store_true', help='measure 10 MB, 100 MB and 1 GB full snapshots')
    parser.add_argument('--minimal-host', action='store_true', help='assert no build tools and an existing system psql 16')
    parser.add_argument('--network-evidence', default='not externally isolated; see separate observation report')
    qualify(parser.parse_args())
