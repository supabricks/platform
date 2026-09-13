"""NE06 deterministic local-index probe of the exact bundled uv and Python.

This is resolver qualification, not a product custom-index feature. The product's
PyPI-only policy is unchanged; product package tests run in release-packages.
"""
import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def qualify(args):
    release = args.release.resolve()
    root = Path(tempfile.mkdtemp(prefix='sb-ne06-index-', dir='/tmp')).resolve()
    root.chmod(0o700)
    report = dict(status='running', checks=[], release_sha256=digest(release / 'release.json'),
                  scope='bundled resolver and interpreter; product registry policy is not overridden')
    web = root / 'web'; web.mkdir()
    requests = []
    mode = {'fail': False}

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append(self.path)
            if mode['fail']:
                self.send_error(503)
            else:
                super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(web)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    index = f'http://127.0.0.1:{server.server_port}/simple'
    python = release / 'python/runtime/bin/python3.12'
    uv = release / 'helpers/uv'
    home = root / 'home'; home.mkdir(mode=0o700)
    env = dict(HOME=str(home), PATH='', UV_CACHE_DIR=str(root / 'cache'),
               UV_PYTHON_DOWNLOADS='never', UV_HTTP_TIMEOUT='2', UV_HTTP_RETRIES='0', PYTHONDONTWRITEBYTECODE='1')

    def run(*parts, success=True):
        result = subprocess.run([str(uv), '--no-config', '--no-python-downloads', *map(str, parts)],
                                cwd=root, env=env, capture_output=True, timeout=25)
        (root / 'private-resolver.log').write_bytes(result.stdout + result.stderr)
        assert (result.returncode == 0) == success, result.stderr.decode(errors='replace')[-2000:]
        return result.stdout

    def project(name, requirement):
        folder = root / name; folder.mkdir()
        (folder / 'pyproject.toml').write_text('[project]\nname="ne06-index-fixture"\nversion="0.1.0"\nrequires-python="==3.12.13"\ndependencies=' + json.dumps(requirement) + '\n[tool.uv]\npackage=false\n[tool.uv.workspace]\n')
        return folder

    def check(name):
        report['checks'].append(name); print(name, flush=True)

    try:
        subprocess.run([str(release / 'bin/supabricks'), 'installation', 'verify'], check=True, stdout=subprocess.DEVNULL)
        wheels = {}
        for package, version in [('humanize', '4.13.0'), ('xxhash', '3.5.0')]:
            wheel = next((release / 'python/notebooks/wheelhouse').glob(f'{package}-{version}-*.whl'))
            shutil.copy2(wheel, web / wheel.name)
            page = web / 'simple' / package; page.mkdir(parents=True)
            page.joinpath('index.html').write_text(f'<a href="../../{wheel.name}#sha256={digest(wheel)}">{wheel.name}</a>')
            wheels[wheel.name] = digest(wheel)
        valid = project('valid', ['humanize==4.13.0', 'xxhash==3.5.0'])
        run('lock', '--project', valid, '--python', python, '--default-index', index, '--no-build')
        requirements = root / 'requirements.txt'
        requirements.write_bytes(run('export', '--project', valid, '--python', python, '--locked', '--no-build', '--no-emit-project', '--format', 'requirements-txt'))
        target = root / 'venv'
        run('--offline', 'venv', '--python', python, target)
        run('pip', 'sync', '--python', target / 'bin/python', '--default-index', index, '--only-binary', ':all:', '--require-hashes', '--link-mode', 'copy', requirements)
        subprocess.run([str(target / 'bin/python'), '-I', '-B', '-c', "import humanize,xxhash;assert humanize.__version__=='4.13.0';assert xxhash.xxh32('x').intdigest()>0"], env=env, check=True)
        assert any(p.startswith('/simple/') for p in requests) and any('.whl' in p for p in requests)
        check('controlled_index_resolves_and_installs_hash_locked_pure_and_native_wheels_with_bundled_tools')
        impossible = project('conflict', ['humanize==0.0.0'])
        run('lock', '--project', impossible, '--python', python, '--default-index', index, '--no-build', success=False)
        assert not (impossible / 'uv.lock').exists()
        check('unsatisfiable_requirement_does_not_publish_lock')
        # A source-only project must not execute its build backend on the user
        # machine. This deliberately observable backend belongs to this fixture.
        marker = root / 'backend-ran'
        source = web / 'ne06_source_fixture-0.1.0.tar.gz'
        code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('fixture backend executed')\n".encode()
        with tarfile.open(source, 'w:gz') as archive:
            info = tarfile.TarInfo('ne06_source_fixture-0.1.0/setup.py')
            info.size = len(code); archive.addfile(info, io.BytesIO(code))
        page = web / 'simple/ne06-source-fixture'; page.mkdir()
        (page / 'index.html').write_text(f'<a href="../../{source.name}#sha256={digest(source)}">{source.name}</a>')
        source_project = project('source-only', ['ne06-source-fixture==0.1.0'])
        run('lock', '--project', source_project, '--python', python, '--default-index', index, '--no-build', success=False)
        assert not marker.exists() and not (source_project / 'uv.lock').exists()
        check('source_only_dependency_refused_without_executing_build_backend')
        corrupt = root / 'corrupt.txt'
        corrupt.write_text('humanize==4.13.0 --hash=sha256:' + '0'*64 + '\n')
        run('--no-cache', 'pip', 'sync', '--python', target / 'bin/python', '--default-index', index, '--only-binary', ':all:', '--require-hashes', '--reinstall', corrupt, success=False)
        subprocess.run([str(target / 'bin/python'), '-I', '-B', '-c', "import humanize;assert humanize.__version__=='4.13.0'"], env=env, check=True)
        check('download_hash_mismatch_is_rejected_before_existing_package_replacement')
        shutil.rmtree(root / 'cache')
        mode['fail'] = True
        failed = project('server-failure', ['humanize==4.13.0'])
        start = time.monotonic()
        run('lock', '--project', failed, '--python', python, '--default-index', index, '--no-build', success=False)
        assert not (failed / 'uv.lock').exists()
        report['failure_seconds'] = round(time.monotonic()-start, 3)
        check('index_503_fails_with_bounded_no_retry_policy_and_no_published_lock')
        mode['fail'] = False
        shutil.rmtree(root / 'cache', ignore_errors=True)
        before = len(requests)
        offline = project('offline', ['humanize==4.13.0'])
        run('--offline', 'lock', '--project', offline, '--python', python, '--default-index', index, '--no-build', success=False)
        assert len(requests) == before and not (offline / 'uv.lock').exists()
        check('cold_offline_resolution_makes_zero_index_requests')
        report.update(status='passed', wheels=wheels, uv_sha256=digest(uv), python_sha256=digest(python))
    except BaseException as error:
        report.update(status='failed', failure_type=type(error).__name__)
        raise
    finally:
        server.shutdown(); server.server_close()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(dict(status=report['status'], private_workspace=str(root))), flush=True)
        if report['status'] == 'passed': shutil.rmtree(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    qualify(parser.parse_args())
