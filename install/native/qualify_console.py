#!/usr/bin/env python3
"""Install the exact archive via signed localhost curl, then qualify in Chromium."""
import argparse
from functools import partial
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from qualify import Handler, run
from stage import stage


def qualify(args):
    workspace = Path(tempfile.mkdtemp(prefix='sb-c01-release-', dir='/tmp'))
    web = workspace / 'web'
    web.mkdir()
    prefix = workspace / "programs ' with spaces"
    key = workspace / 'preview.pem'
    server = None
    succeeded = False
    try:
        for path in args.directory.glob(f'supabricks-{args.version}-*'):
            if path.name.endswith(('.tar.gz', '.tar.gz.sha256')):
                shutil.copy2(path, web / path.name)
        run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', key])
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(web)))
        base = f'http://127.0.0.1:{server.server_port}'
        stage(web, args.version, base, key)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ, SUPABRICKS_INSTALL_DIR=str(prefix), SUPABRICKS_NO_MODIFY_PATH='1',
                   SUPABRICKS_DATA_DIR=str(workspace / 'installer-data'))
        curl = subprocess.Popen(['curl', '-fsSL', base + '/install.sh'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        bash = subprocess.run(['bash'], stdin=curl.stdout, capture_output=True, text=True, env=env, timeout=240)
        curl.stdout.close()
        curl.wait(timeout=10)
        if curl.returncode or bash.returncode:
            raise RuntimeError('signed console installer failed: ' + bash.stderr[-1500:])
        subprocess.run([args.node, args.harness, '--binary', str(prefix / 'bin/supabricks'),
                        '--report', str(args.report), '--screenshot', str(args.report.with_suffix('.png'))], check=True, env=env)
        succeeded = True
    finally:
        if server:
            server.shutdown()
            server.server_close()
        # The browser harness owns/stops its own separate data root.
        key.unlink(missing_ok=True)
        if succeeded:
            shutil.rmtree(workspace)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--version', default='v0.1.0-alpha.4')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--node', required=True)
    parser.add_argument('--harness', required=True)
    qualify(parser.parse_args())
